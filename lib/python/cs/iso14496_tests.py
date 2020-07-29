#!/usr/bin/python
#
# Unit tests for cs.iso14496.
#   - Cameron Simpson <cs@cskk.id.au> 06may2017
#

from __future__ import print_function
import sys
import os
import os.path
import unittest
from .binary_tests import _TestPacketFields
from .fileutils import read_from
from .logutils import D
from .iso14496 import parse_file
from . import iso14496 as iso14496_module
from .x import X

TESTFILE = 'TEST.mp4'

class Test_iso14496(unittest.TestCase):
  ''' Test `cs.iso14496`.
  '''

  @unittest.skipUnless(os.path.exists(TESTFILE), 'no ' + TESTFILE)
  def test(self):
    S = os.stat(TESTFILE)
    mp4_size = S.st_size
    total_blocks = 0
    with open(TESTFILE, 'rb') as mp4fp:
      total_size = 0
      for B in parse_file(mp4fp):
        total_blocks += 1
        total_size += B.length
        X("Box %s, %d bytes", bytes(B.box_type), B.length)
    X(
        "%s: total top level blocks = %d, total_size = %d", TESTFILE,
        total_blocks, total_size
    )
    self.assertEqual(
        total_size, mp4_size,
        "file size = %d, frames total = %d" % (mp4_size, total_size)
    )

class TestISO14496PacketFields(_TestPacketFields, unittest.TestCase):
  ''' Test the `PacketField`s in `cs.iso14496`.
      Subclasses `cs.binary_tests._TestPacketFields`
      which locates all `PacketFields` in the associated module.
  '''

  def setUp(self):
    ''' We're testing the cs.binary module.
    '''
    self.module = iso14496_module

def selftest(argv, **kw):
  sys.argv = argv
  unittest.main(__name__, defaultTest=None, argv=argv, failfast=True, **kw)

if __name__ == '__main__':
  selftest(sys.argv)
