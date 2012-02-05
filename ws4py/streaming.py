# -*- coding: utf-8 -*-
import struct

from ws4py.utf8validator import Utf8Validator
from ws4py.messaging import TextMessage, BinaryMessage, CloseControlMessage,\
     PingControlMessage, PongControlMessage
from ws4py.framing import Frame, OPCODE_CONTINUATION, OPCODE_TEXT, \
     OPCODE_BINARY, OPCODE_CLOSE, OPCODE_PING, OPCODE_PONG
from ws4py.exc import FrameTooLargeException, ProtocolException, InvalidBytesError,\
     TextFrameEncodingException, UnsupportedFrameTypeException, StreamClosed

class Stream(object):
    def __init__(self):
        """
        Represents a websocket stream of bytes flowing in and out.

        The stream doesn't know about the data provider itself and
        doesn't even know about sockets. Instead the stream simply
        yields for more bytes whenever it requires it. The stream owner
        is responsible to provide the stream with those bytes until
        a frame can be interpreted.

        >>> s = Stream()
        >>> s.parser.send(BYTES)
        >>> s.has_messages
        False
        >>> s.parser.send(MORE_BYTES)
        >>> s.has_messages
        True
        >>> s.messages.pop()
        <TextMessage ... >
        
        """
        self.message = None
        """
        Parsed test or binary messages. Whenever the parser
        reads more bytes from a fragment message, those bytes
        are appended to the most recent message.
        """

        self.pings = []
        """
        Parsed ping control messages. They are instances of
        messaging.PingControlMessage
        """
        
        self.pongs = []
        """
        Parsed pong control messages. They are instances of
        messaging.PongControlMessage
        """
        
        self.closing = None
        """
        Parsed close control messsage. Instance of
        messaging.CloseControlMessage
        """
        
        self.errors = []
        """
        Detected errors while parsing. Instances of
        messaging.CloseControlMessage
        """
        
        self.parser = self.receiver()
        """
        Parser in charge to process bytes it is fed with.
        """

        # Python generators must be initialized once.
        self.parser.next()


    def text_message(self, text):
        """
        Returns a messaging.TextMessage instance
        ready to be built. Convenience method so
        that the caller doesn't need to import the
        TextMessage class itself.

        @param text: data to be carried by the message
        """
        return TextMessage(text=text)

    def binary_message(self, bytes):
        """
        Returns a messaging.BinaryMessage instance
        ready to be built. Convenience method so
        that the caller doesn't need to import the
        BinaryMessage class itself.

        @param text: data to be carried by the message
        """
        return BinaryMessage(bytes)

    @property
    def has_message(self):
        """
        Checks if the stream has received any message
        which, if fragmented, is completed.
        """
        if self.message is not None:
            return self.message.completed

        return False

    def close(self, code=1000, reason=''):
        """
        Returns a close control message built from
        a messaging.CloseControlMessage instance.

        @param code: closing status code
        @param reason: status message
        @return: bytes representing a close control single framed message
        """
        return CloseControlMessage(code=code, reason=reason)

    def ping(self, data=''):
        """
        Returns a ping control message built from
        a messaging.PingControlMessage instance.

        @param data: ping data
        @return: bytes representing a ping single framed message
        """
        return PingControlMessage(data).single()

    def pong(self, data=''):
        """
        Returns a ping control message built from
        a messaging.PongControlMessage instance.

        @param data: pong data
        @return: bytes representing a pong single framed message
        """
        return PongControlMessage(data).single()

    def receiver(self):
        """
        Parser that keeps trying to interpret bytes it is fed with as
        incoming frames part of a message.

        Control message are single frames only while data messages, like text
        and binary, may be fragmented accross frames.

        The way it works is by instanciating a framing.Frame object,
        then running its parser generator which yields how much bytes
        it requires to performs its task. The stream parser yields this value
        to its caller and feeds the frame parser.

        When the frame parser raises StopIteration, the stream parser
        tries to make sense of the parsed frame. It dispatches the frame's bytes
        to the most appropriate message type based on the frame's opcode.

        Overall this makes the stream parser totally agonstic to
        the data provider.
        """
        utf8validator = Utf8Validator()
        running = True
        while running:
            frame = Frame()
            while True:
                try:
                    bytes = (yield frame.parser.next())
                    if bytes is None:
                        raise InvalidBytesError()
                    
                    frame.parser.send(bytes)
                except StopIteration:
                    bytes = frame.body or ''
                    if frame.masking_key:
                        bytes = frame.unmask(bytes)
                    else:
                        bytes = bytearray(frame.body)

                    if frame.opcode == OPCODE_TEXT:
                        if self.message and not self.message.completed:
                            # We got a text frame before we completed the previous one
                            self.errors.append(CloseControlMessage(code=1002))
                            break

                        is_valid, _, _, _ = utf8validator.validate(bytes)
                        
                        if is_valid or (not is_valid and frame.fin == 0):
                            m = TextMessage(bytes)
                            m.completed = (frame.fin == 1)
                            self.message = m
                        elif not is_valid and frame.fin == 1:
                            self.errors.append(CloseControlMessage(code=1007))

                    elif frame.opcode == OPCODE_BINARY:
                        m = BinaryMessage(bytes)
                        m.completed = (frame.fin == 1)
                        self.message = m

                    elif frame.opcode == OPCODE_CONTINUATION:
                        m = self.message
                        if m is None:
                            self.errors.append(CloseControlMessage(code=1002))
                            break
                        
                        m.completed = (frame.fin == 1)
                        if m.opcode == OPCODE_TEXT:
                            is_valid, _, _, _ = utf8validator.validate(bytes)
                            if is_valid:
                                m.extend(bytes)
                            else:
                                self.errors.append(CloseControlMessage(code=1007))
                            #except UnicodeDecodeError:
                            #    self.errors.append(CloseControlMessage(code=1007))
                            #    break
                        else:
                            m.extend(bytes)

                    elif frame.opcode == OPCODE_CLOSE:
                        code = 1000
                        reason = ""
                        if len(bytes) == 0:
                            self.closing = CloseControlMessage(code=1000)
                        elif 1 < len(bytes) < 126:
                            code = struct.unpack("!H", str(bytes[0:2]))[0]
                            try:
                                code = int(code)
                            except TypeError:
                                code = 1002
                                reason = 'Invalid Closing Frame Code Type'
                            else:
                                # Those codes are reserved or plainly forbidden
                                if code < 1000 or code in [1004, 1005, 1006, 1012, 1013, 1014, 1015,
                                                           1016, 1100, 2000, 2999, 5000, 65536]:
                                    code = 1002
                                    reason = 'Invalid Closing Frame Code'
                                else:    
                                    if len(bytes) > 2:
                                        try:
                                            msg = bytes[2:] if frame.masking_key else frame.body[2:]
                                            reason = msg.decode("utf-8")
                                        except UnicodeDecodeError:
                                            code = 1007
                                            reason = ''                                
                            self.closing = CloseControlMessage(code=code, reason=reason)
                        else:
                            self.errors.append(CloseControlMessage(code=1002))
                        
                    elif frame.opcode == OPCODE_PING:
                        self.pings.append(PingControlMessage(bytes))

                    elif frame.opcode == OPCODE_PONG:
                        self.pongs.append(PongControlMessage(bytes))
                    
                    else:
                        self.errors.append(CloseControlMessage(code=1003))

                    # When the frame's payload is empty, we must yield
                    # once more so that the caller is properly aligned
                    if not bytes:
                        yield 0

                    break

                except ProtocolException:
                    self.errors.append(CloseControlMessage(code=1002))
                except FrameTooLargeException:
                    self.errors.append(CloseControlMessage(code=1002))
                except StreamClosed:
                    running = False
                    break

            frame.parser.close()
            utf8validator.reset()
            
        utf8validator = None
