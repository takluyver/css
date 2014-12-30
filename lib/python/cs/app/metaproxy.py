#!/usr/bin/python
#
# MetaProxy: content rewriting agressive cache web proxy toolkit.
#   - Cameron Simpson <cs@zip.com.au> 26dec2014
#
# Design:
#  local squid:
#       optional: redirect or rewrite specific domains to domain.metaproxy,
#         often http: for local caching and speed
#       divert *.metaproxy directly to trusted remote squid
#
#  remote-personal-squid
#       divert (.metaproxy to local remote-personal-metaproxy
#
#  remote-personal-metaproxy
#       rewrite rq URL to target URL, often https:
#       fetch, parse, return (usually cachable) content
#
# TODO:
#   cookie translation: domain<->domain.metaproxy
#

from __future__ import print_function
import sys
import os
import os.path
try:
  import socketserver
except ImportError:
  import SocketServer as socketserver
import socket
from threading import Thread
from cs.excutils import LogExceptions
from cs.logutils import setup_logging, Pfx, debug, info, warning, error, D, X
from cs.later import Later
from cs.lex import get_hexadecimal
from cs.rfc2616 import read_headers, message_has_body, pass_chunked, pass_length, \
                       dec8, enc8, CRLF, CRLFb
from cs.seq import Seq
from cs.obj import O

USAGE = '''Usage: %s address:port'''

DEFAULT_PORT = 3128
DEFAULT_PARALLEL = 4

def main(argv):
  cmd = os.path.basename(argv.pop(0))
  usage = USAGE % (cmd,)
  setup_logging(cmd)
  badopts = False
  if not argv:
    warning("missing address:port")
    badopts = True
  else:
    addrport = argv.pop(0)
    try:
      addr, port = addrport.rsplit(':', 1)
    except ValueError:
      warning("invalid address:port, no colon: %r", addrport)
      badopts = True
    else:
      try:
        port = int(port)
      except ValueError:
        warning("invalid address:port, port must be an integer: %r", addrport)
        badopts = True
  if argv:
    warning("extra arguments after address:port: %r", argv)
    badopts = True
  if badopts:
    print(usage, file=sys.stderr)
    return 2
  P = MetaProxy(addr, port)
  P.serve_forever()
  return 0

def openpair(fd):
  ''' Open the supplied file descriptor `fd` for read and open a dup() of it for write.
  '''
  fd2 = os.dup(fd)
  fpin = os.fdopen(fd, "rb")
  fpout = os.fdopen(fd2, "wb")
  return fpin, fpout

class MetaProxy(socketserver.TCPServer):

  def __init__(self, addr, port=DEFAULT_PORT, parallel=DEFAULT_PARALLEL):
    self.allow_reuse_address = True
    socketserver.TCPServer.__init__(
            self,
            (addr, port),
            MetaProxyHandler
          )
    self.later = Later(parallel)
    self.later.open()
    self.name = "MetaProxy(addr=%s,port=%s)" % (addr, port)
    self.tagseq = Seq()

  def __str__(self):
    return self.name

  def shutdown(self):
    self.later.close()
    socketserver.TCPServer.shutdown(self)

  # TODO: just use a semaphore? or will we want prioritisation later anyway?
  def process_request(self, rqf, client_addr):
    handler = self.RequestHandlerClass(rqf, client_addr, self)
    self.later.defer(self._process_request, handler, rqf, client_addr)

  def _process_request(self, handler, rqf, client_addr):
    with LogExceptions():
      try:
        handler.handle(rqf, client_addr)
        self.shutdown_request(rqf)
      except:
        self.handle_error(rqf, client_addr)
        self.shutdown_request(rqf)

  def newtag(self, prefix):
    ''' Allocate a new tag for labelling requests.
    '''
    n = next(self.tagseq)
    return "%s-%d" % (prefix, n)

class MetaProxyHandler(socketserver.BaseRequestHandler):
  ''' Request handler class for MetaProxy TCPServers.
  '''

  def __init__(self, sockobj, localaddr, proxy):
    socketserver.BaseRequestHandler(sockobj, localaddr, proxy)
    self.proxy = proxy
    self.localaddr = localaddr
    self.remoteaddr = sockobj.getpeername()

  def __str__(self):
    return "%s.MetaProxyHandler(%s<==>%s)" % (self.proxy,
                                              self.localaddr,
                                              self.remoteaddr)

  def handle(self, rqf, client_addr):
    ''' Handle a connection to the proxy.
    '''
    fpin, fpout = openpair(rqf.fileno())
    self._proxy_requests(fpin, fpout)

  def _proxy_requests(self, fpin, fpout):
    ''' Process multiple HTTP proxy requests on the same pair of data streams.
    '''
    client_tag = self.proxy.newtag("client")
    with Pfx(client_tag):
      while True:
        info("read new request...")
        httprq = dec8(fpin.readline())
        if not httprq:
          info("end of client requests")
          break
        method, uri, version = httprq.split()
        RQ = URI_Request(self, method, uri, version)
        info("new request: %s", RQ)
        with Pfx(str(RQ)):
          uri_scheme, uri_tail = uri.split(':', 1)
          req_header_data, req_headers = read_headers(fpin)
          RQ.req_header_data = req_header_data
          RQ.req_headers = req_headers
          proxy_addrport = RQ.choose_proxy()
          upstream = socket.create_connection(proxy_addrport)
          fpup_in, fpup_out = openpair(upstream.fileno())
          Tdownstream = Thread(name="copy from upstream",
                               target=RQ.pass_response,
                               args=(client_tag, fpup_in, fpout))
          Tdownstream.daemon = True
          Tdownstream.start()
          RQ.pass_request(fpin, fpup_out)
          fpup_out.flush()
          Tdownstream.join()
          fpout.flush()
          # disconnect from proxy
          fpup_out.close()
          fpup_in.close()
          upstream.close()
          upstream = None
          info("upstream closed")
      info("end proxy requests")

class URI_Request(O):

  def __init__(self, handler, method, uri, version):
    ''' An object for tracking state of a request.
    '''
    self.handler = handler
    self.name = handler.proxy.newtag("rq")
    self.req_method = method
    self.req_uri = uri
    self.req_http_version = version

  def __str__(self):
    return "%s[%s:%s]" % (self.name, self.req_method, self.req_uri)

  def choose_proxy(self):
    ''' Decide where to connect to deliver this request.
    '''
    proxy_addrport = '127.0.0.1', 3128
    info("chose proxy %r", proxy_addrport)
    return proxy_addrport

  @property
  def req_has_body(self):
    return message_has_body(self.req_headers)

  @property
  def rsp_has_body(self):
    ''' Does this response have a message body to forward?
        See RFC2616, part 4.3.
    '''
    if self.req_method == 'HEAD':
      return False
    code = self.rsp_code
    if code.startswith('1') or code == '204' or code == '304':
      return False
    return True

  def pass_request(self, fpin, fpout):
    for s in self.req_method, ' ', self.req_uri, ' ', self.req_http_version:
      fpout.write(enc8(s))
    fpout.write(CRLFb)
    fpout.write(self.req_header_data)
    fpout.write(CRLFb)
    with Pfx("request body"):
      transfer_encoding = self.req_headers.get('Transfer-Encoding')
      content_length = self.req_headers.get('Content-Length')
      if transfer_encoding is not None:
        transfer_encoding = transfer_encoding.strip().lower()
        if transfer_encoding == 'identity':
          debug("pass_identity")
          self.pass_identity(fpin, fpout)
        else:
          hdr_trailer = self.req_headers.get('Trailer')
          debug("pass_chunked")
          pass_chunked(fpin, fpout, hdr_trailer)
      elif content_length is not None:
        length = int(content_length.strip())
        debug("pass_length")
        pass_length(fpin, fpout, length)
      else:
        debug("no body expected")

  def pass_response(self, client_tag,  fpin, fpout):
    with Pfx("%s: pass_response", client_tag):
      info("read server response...")
      bline = fpin.readline()
      line = dec8(bline)
      if not line.endswith(CRLF):
        raise ValueError("truncated response (no CRLF): %r" % (line,))
      http_version, status_code, reason = line.split(' ', 2)
      info("server response: %s %s %s", http_version, status_code, reason)
      self.rsp_http_version = http_version
      self.rsp_status_code = status_code
      self.rsp_reason = reason
      fpout.write(bline)
      rsp_header_data, rsp_headers = read_headers(fpin)
      debug("response headers:\n%s\n", rsp_headers.as_string())
      self.rsp_header_data = rsp_header_data
      self.rsp_headers = rsp_headers
      fpout.write(rsp_header_data)
      fpout.write(CRLFb)
      with Pfx("response body"):
        if ( self.req_method == 'HEAD'
          or status_code.startswith('1')
          or status_code == '204'
          or status_code == '304'
           ):
          debug("none expected")
        else:
          transfer_encoding = rsp_headers.get('Transfer-Encoding')
          content_length = rsp_headers.get('Content-Length')
          if transfer_encoding is not None:
            transfer_encoding = transfer_encoding.strip().lower()
            if transfer_encoding == 'identity':
              debug("transfer_encoding = %r, using pass_identity", transfer_encoding)
              self.pass_identity(fpin, fpout)
            else:
              debug("transfer_encoding = %r, using pass_chunked", transfer_encoding)
              hdr_trailer = rsp_headers.get('Trailer')
              debug("trailer = %r", hdr_trailer)
              pass_chunked(fpin, fpout, hdr_trailer)
          elif content_length is not None:
            debug("content_length = %r, using pass_length", content_length)
            length = int(content_length.strip())
            pass_length(fpin, fpout, length)
          else:
            debug("no body expected")

if __name__ == '__main__':
  sys.exit(main(sys.argv))
