#!/usr/bin/python -tt
#
# TCP client/server code.
#       - Cameron Simpson <cs@zip.com.au> 07dec2007
#

import os
from socket import socket, SHUT_WR, SHUT_RD
from socketserver import TCPServer, ThreadingMixIn, StreamRequestHandler
from .stream import StreamStore
from cs.fileutils import OpenSocket
from cs.logutils import debug, X
from cs.queues import NestingOpenCloseMixin

class TCPStoreServer(ThreadingMixIn, TCPServer, NestingOpenCloseMixin):
  ''' A threading TCPServer that accepts connections by TCPStoreClients.
  '''

  def __init__(self, bind_addr, S):
    ThreadingTCPServer.__init__(self, bind_addr, _RequestHandler)
    S.open()
    self.S = S

  def shutdown(self):
    self.S.close()

class _RequestHandler(StreamRequestHandler):

  def __init__(self, request, client_address, server):
    self.S = server.S
    StreamRequestHandler.__init__(self, request, client_address, server)

  def handle(self):
    RS = StreamStore(str(self.S),
                     OpenSock(self.request, False),
                     OpenSock(self.request, True),
                     local_store=self.S,
                    )
    RS.join()
    RS.shutdown()

class TCPStoreClient(StreamStore):
  ''' A Store attached to a remote Store at `bind_addr`.
  '''

  def __init__(self, bind_addr):
    self.sock = socket()
    self.sock.connect(bind_addr)
    StreamStore.__init__(self,
                         "TCPStore(%s)" % (bind_addr,),
                         OpenSock(self.sock, False),
                         OpenSock(self.sock, True),
                        )

  def shutdown(self):
    StreamStore.shutdown(self)
    self.sock.close()
