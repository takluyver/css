#!/usr/bin/env python3

''' HTTP access to a Store.
'''

import os
import sys
from flask import (
    Flask, render_template, request, session as flask_session, jsonify, abort
)
from cs.resources import RunStateMixin
from . import defaults
from .hash import MissingHashcodeError, Hash_SHA1

def main(argv=None):
  if argv is None:
    argv = sys.argv
  argv = list(argv)
  name = argv.pop(0)
  if argv:
    host, port = argv
  else:
    host, port = '127.0.0.1', 5000
  app = StoreApp(name, defaults.S)
  app.run(host=host, port=port)

class _StoreApp(Flask, RunStateMixin):
  ''' A Flask application with a `.store` attribute.
  '''

  def __init__(self, name, S):
    Flask.__init__(self, name)
    RunStateMixin.__init__(self)
    self.store = S
    self.secret_key = os.urandom(16)

  def run(self, *a, **kw):
    ''' Call the main Flask.run inside the RunState.
    '''
    with self.runstate:
      with self.store:
        super().run(*a, **kw)

def StoreApp(name, S):
  ''' Factory method to create the app and attach routes.
  '''
  app = _StoreApp(name, S)

  @app.route('/h/<hashcode_s>.sha1')
  def h_sha1(hashcode_s):
    try:
      h = _h(Hash_SHA1.from_hashbytes_hex(hashcode_s))
    except ValueError as e:
      abort(404, 'invalid hashcode')
    return _h(h)

  def _h(hashcode):
    try:
      data = app.store[hashcode]
    except MissingHashcodeError:
      abort(404)
    else:
      rsp = app.make_response(data)
      rsp.headers.set('Content-Type', 'application/octet-stream')
      rsp.headers.set('ETag', hashcode.etag)
      return rsp

  return app

if __name__ == '__main__':
  sys.exit(main(sys.argv))
