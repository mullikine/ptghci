import os
import ast
import sys
import json
import re
import shutil
import zmq
import time
import threading
from .exceptions import GhciException
from .response import Response
from prompt_toolkit import print_formatted_text
from subprocess import Popen, DEVNULL, PIPE
import colors

SYNC_RE = re.compile(r'\d #~PTGHCI~SYNC~(\d+)~#')

class StreamEchoThread(threading.Thread):
    """
    Read from a zmq socket and echo to the appropriate stream (stdout or
    stderr)
    """
    def __init__(self, socket: zmq.Socket, pt_print):
        self.socket = socket
        socket.setsockopt(zmq.SUBSCRIBE, b"")  # Listen to everything
        threading.Thread.__init__(self)
        self.daemon = True
        self.sync_condition = threading.Condition()
        self.last_sync_seq = -1
        self.pt_print = pt_print
        self.interrupted = False

    def run(self):
        while True:
            message = self.socket.recv().decode()

            # Check if we need to increment our counter
            m = SYNC_RE.match(message)
            if m:
                self.last_sync_seq = int(m.group(1))
                with self.sync_condition:
                    self.sync_condition.notify_all()
                self.interrupted = False
            elif not self.interrupted:
                stream, contents = message[0], message[2:]
                if stream == '1':
                    self.pt_print(contents, file=sys.stdout)
                elif stream == '2':
                    self.pt_print(contents, file=sys.stderr)
                else:
                    raise ValueError("Bad stream identifier.  Expected 1 or 2"
                                     " but got "+str(stream))

    def await_sync(self, sync_seq: int):
        with self.sync_condition:
            done = False
            while not done:
                done = self.sync_condition.wait_for(
                    lambda: self.last_sync_seq >= sync_seq,
                    timeout=0.01
                )

    def interrupt(self):
        self.interrupted = True


def _response_from_reply(reply: dict) -> Response:
    if reply['tag'] == 'ExecCaptureResponse':
        if reply['success']:
            return Response.from_value(reply['content'])
        else:
            return Response.from_error_message(reply['content'])
    elif reply['tag'] == 'ExecStreamResponse':
        if reply['success']:
            return Response(Response.Stream)
        else:
            return Response.from_error_message(reply['content'])
    else:
        raise ValueError("Unexpected response type: %s" % reply['tag'])


class Engine():
    """
    Engine manages the connection with ptghci-engine, the Haskell interface
    to GHCi.
    """
    def __init__(self, config, pt_print=print_formatted_text):

        self.ctx = zmq.Context.instance()
        self.comm_socket = self.ctx.socket(zmq.REQ)
        self.ctrl_socket = self.ctx.socket(zmq.PAIR)
        self.iopub_socket = self.ctx.socket(zmq.SUB)
        self.comm_socket.setsockopt(zmq.constants.REQ_CORRELATE, 1)
        self.comm_socket.setsockopt(zmq.constants.REQ_RELAXED, 1)
        comm_addr = os.environ['PTGHCI_REQUEST_ADDR']
        ctrl_addr = os.environ['PTGHCI_CONTROL_ADDR']
        iopub_addr = os.environ['PTGHCI_IOPUB_ADDR']
        self.comm_socket.connect(comm_addr)
        self.ctrl_socket.connect(ctrl_addr)
        self.iopub_socket.connect(iopub_addr)
        # comm_port = self.comm_socket.bind_to_random_port("tcp://127.0.0.1")
        # ctrl_port = self.ctrl_socket.bind_to_random_port("tcp://127.0.0.1")
        # stdout_port = self.iopub_socket.bind_to_random_port("tcp://127.0.0.1")

        self.iopub_thread = StreamEchoThread(self.iopub_socket, 
                                             pt_print=pt_print)

        self.iopub_thread.start()


    @classmethod
    def oop_engine(cls, config, pt_print=print_formatted_text):
        """
        Used in testing - spin up the Haskell engine in a separate process and
        connect to it
        """
        self = super().__new__(cls)
        self.ctx = zmq.Context.instance()
        self.comm_socket = self.ctx.socket(zmq.REQ)
        self.ctrl_socket = self.ctx.socket(zmq.PAIR)
        self.iopub_socket = self.ctx.socket(zmq.SUB)
        self.comm_socket.setsockopt(zmq.constants.REQ_CORRELATE, 1)
        self.comm_socket.setsockopt(zmq.constants.REQ_RELAXED, 1)

        engine_bin = shutil.which('ptghci')
        if engine_bin is None:
            if 'PTGHCI_ENGINE' in os.environ:
                engine_bin = os.environ['PTGHCI_ENGINE']
            else:
                raise FileNotFoundError(
                    "ptghci not on PATH and PTGHCI_ENGINE environment"
                    " variable not defined")

        # Start the engine
        args = [engine_bin]

        env = os.environ
        env['PTGHCI_ENGINE_MODE'] = '1'
        self._proc = Popen(args, stdin=DEVNULL, stdout=PIPE,
                           bufsize=1, universal_newlines=True)
        
        endpoints_line = self._proc.stdout.readline()
        comm_addr, ctrl_addr, iopub_addr = ast.literal_eval(endpoints_line)
        self.comm_socket.connect(comm_addr)
        self.ctrl_socket.connect(ctrl_addr)
        self.iopub_socket.connect(iopub_addr)
        
        self.iopub_thread = StreamEchoThread(self.iopub_socket,
                                             pt_print=pt_print)

        self.iopub_thread.start()
        return self

    def execute(self, code: str):
        """
        Execute code in GHCi and capture the response
        """
        self._await_send(json.dumps({
            'content': code,
            'tag': 'RequestExecCapture'}).encode())
        try:
            return _response_from_reply(self._await_reply())
        except KeyboardInterrupt:
            self.send_interrupt()
            raise

    def exec_stream(self, code: str):
        self._await_send(json.dumps({
            'content': code,
            'tag': 'RequestExecStream'}).encode())
        message = self._await_reply()
        if message['success']:
            # Wait for the sync message
            # What if we get interrupted? 
            try:
                self.iopub_thread.await_sync(message['syncVal'])
            except KeyboardInterrupt as interrupt:
                self.send_interrupt()

        return _response_from_reply(message)


    def find_doc(self, identifier):
        self._await_send(json.dumps({'identifier': identifier,
                                          'tag': 'RequestOpenDoc'}).encode())
        return _response_from_reply(self._await_reply())

    def get_type(self, identifier, show_hole_fits=True):
        """ Gets the result of :t <identifier> """
        self._await_send(json.dumps({'identifier': identifier,
                                     'showHoleFits': show_hole_fits,
                                     'tag': 'RequestType'}).encode())
        return _response_from_reply(self._await_reply())

    def get_completions(self, line_before_cursor):
        """ Gets the result of :complete repl <line>"""
        self._await_send(json.dumps({'lineBeforeCursor': line_before_cursor,
                                     'tag': 'RequestCompletion'}).encode())
        reply = self._await_reply()
        if reply.get('success'):
            return reply['startChars'], reply['candidates']
        else:
            return None


    def find_source(self, identifier):
        self._await_send(json.dumps({
            'identifier': identifier,
            'tag': 'RequestOpenSource'}).encode())
        return _response_from_reply(self._await_reply())

    def get_load_messages(self):
        """
        Request the loading messages for GHCi from the engine
        """
        self._await_send(
            json.dumps({'tag': 'RequestLoadMessages'}).encode())
        message = self._await_reply()
        load_msgs = message['messages']
        lines = []
        for msg in load_msgs:
            if msg.get('tag') == 'Loading':
                lines.append("Loaded %s from %s" % (msg['loadModule'],
                                                    msg['loadFile']))
            elif msg.get('tag') == 'Message':
                severity = colors.color(
                    msg['loadSeverity'],
                    style='bold',
                    fg='yellow' if msg['loadSeverity'] == 'Warning'
                    else 'red')
                startPos = tuple(msg['loadFilePos'])
                endPos = tuple(msg['loadFilePosEnd'])
                lines.append("%s: at %s%s%s"
                             % (severity,
                                msg['loadFile'],
                                '' if startPos == (0, 0)
                                else ' '+str(startPos),
                                '' if endPos == startPos
                                else "-"+str(endPos)))
                lines.extend([('   '+m) for m in msg['loadMessage']])
            elif msg.get('tag') == 'LoadConfig':
                lines.append('Loading GHCI configuration from %s'
                             % msg['loadFile'])
            elif msg.get('tag') == 'LoadGhciVersion':
                lines.append('ptGHCi running GHCi version ' + msg['loadVersion'])
            # elif msg.get('tag') == 'LoadUnknown':
            #     lines.append(msg['loadUnknownMessage'])
        return '\n'.join(lines)

    def _await_send(self, msg):
        poller = zmq.Poller()
        poller.register(self.comm_socket, zmq.POLLOUT)

        while True:
            pollres = poller.poll(100)
            if pollres:
                return self.comm_socket.send(msg)


    def _await_reply(self):
        poller = zmq.Poller()
        poller.register(self.comm_socket, zmq.POLLIN)

        # As Ctrl-C seems to be less-than-reliable when waiting for recv,
        # particularly on Windows, poll here to ensure we can escape.
        while True:
            try:
                # message = self.comm_socket.recv()
                pollres = poller.poll(100)
                #print("POLLRES", pollres)
                if pollres:
                    message = self.comm_socket.recv()
                    return json.loads(message.decode('utf-8'))
                time.sleep(.001)
            except KeyboardInterrupt as interrupt:
                self.send_interrupt()
                # After an interrupt, we should get a confirmation response
                while True:
                    try:
                        if poller.poll(100):
                            message = self.comm_socket.recv()
                            break
                    except KeyboardInterrupt:
                        print("Re-sending interrupt")
                        self.send_interrupt()
                raise interrupt

    def send_interrupt(self):
        self.ctrl_socket.send(b'Interrupt')
        self.iopub_thread.interrupt()

    def finish(self):
        try:
            self._proc.kill()
        except AttributeError:
            pass
