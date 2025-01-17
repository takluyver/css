#!/usr/bin/python
#
# Blockify tests.
#       - Cameron Simpson <cs@cskk.id.au>
#

''' Unit tests for cs.vt.blockify.
'''

from itertools import chain
import os
import os.path
import sys
import unittest
from cs.buffer import CornuCopyBuffer
from cs.fileutils import read_from
from cs.randutils import randomish_chunks
from .blockify import (
    blockify,
    blocked_chunks_of,
    blocked_chunks_of2,
    DEFAULT_SCAN_SIZE,
)
from .parsers import scan_text, scan_mp3, scan_mp4
from .scan import MAX_BLOCKSIZE
from .store import MappingStore

QUICK = len(os.environ.get('QUICK', '')) > 0

PARSERS = scan_text, scan_mp3, scan_mp4
SCAN_TESTFILES = {
    scan_text: ('CS_VT_BLOCKIFY_TESTS__TESTFILE_TEXT', __file__),
    scan_mp3: ('CS_VT_BLOCKIFY_TESTS__TESTFILE_MP3', 'TEST.mp3'),
    scan_mp4: ('CS_VT_BLOCKIFY_TESTS__TESTFILE_MP4', 'TEST.mp4'),
}

def scanner_testfile(parser):
  ''' Return the filename to scan for a specified `parser`, or `None`.
  '''
  try:
    envvar, default_filename = SCAN_TESTFILES[parser]
  except KeyError:
    return None
  return os.environ.get(envvar, default_filename)

class BlockifyTestMixin:
  ''' All the unit tests.
  '''

  # load my_code from this test suite
  with open(__file__, 'rb') as myfp:
    mycode = myfp.read()

  # generate some random data
  if QUICK:
    random_data = list(randomish_chunks(1200, limit=12))
  else:
    random_data = list(randomish_chunks(12000, limit=1280))

  def test01scanners(self):
    ''' Test some domain specific data parsers.
    '''
    for parser in PARSERS:
      with self.subTest(parser.__name__):
        f = None
        testfilename = scanner_testfile(parser)
        if testfilename is None:
          input_chunks = self.random_data
        else:
          self.assertIsNotNone(testfilename)
          f = open(testfilename, 'rb')
          input_chunks = read_from(f)
        last_offset = 0
        for offset in parser(CornuCopyBuffer(input_chunks)):
          self.assertTrue(
              last_offset <= offset,
              "offset %d <= last_offset %d" % (offset, last_offset)
          )
          last_offset = offset
        if f is not None:
          f.close()
          f = None

  def test02blocked_chunks_of(self):
    ''' Blockify some input sources.
    '''
    for parser in [None] + list(PARSERS):
      testfilename = None if parser is None else scanner_testfile(parser)
      if testfilename is None:
        self._test_blocked_chunks_of(
            parser, '100 x ' + __file__, [self.mycode for _ in range(100)]
        )
        self._test_blocked_chunks_of(parser, 'random data', self.random_data)
      else:
        with open(testfilename, 'rb') as f:
          input_chunks = read_from(f, DEFAULT_SCAN_SIZE)
          self._test_blocked_chunks_of(parser, testfilename, input_chunks)

  def _test_blocked_chunks_of(self, parser, input_desc, input_chunks):
    with self.subTest(self.BLOCKED.__name__, parser=parser, source=input_desc):
      source_chunks = list(input_chunks)
      src_total = sum(map(len, source_chunks))
      chunk_total = 0
      nchunks = 0
      all_chunks = []
      offset = 0
      for chunk in self.BLOCKED(source_chunks, scanner=parser):
        nchunks += 1
        chunk_total += len(chunk)
        all_chunks.append(chunk)
        offset += len(chunk)
        self.assertTrue(
            len(chunk) <= MAX_BLOCKSIZE,
            "len(chunk)=%d > MAX_BLOCKSIZE=%d" % (len(chunk), MAX_BLOCKSIZE)
        )
        if src_total is not None:
          self.assertTrue(
              chunk_total <= src_total,
              "chunk_total:%d > src_total:%d" % (chunk_total, src_total)
          )
      if src_total is not None:
        self.assertEqual(src_total, chunk_total)
        self.assertEqual(b''.join(source_chunks), b''.join(all_chunks))

  def test03blockifyAndRetrieve(self):
    ''' Blockify some data and ensure that the blocks match the data.
    '''
    with MappingStore("TestAll.test00blockifyAndRetrieve", {}):
      with open(__file__, 'rb') as f:
        data = f.read()
      blocks = list(blockify([data]))
      data2 = b''.join(chain(*blocks))
      self.assertEqual(
          len(data), len(data2), "data mismatch: len(data)=%d, len(data2)=%d" %
          (len(data), len(data2))
      )
      self.assertEqual(
          data, data2,
          "data mismatch: data and data2 same length but contents differ"
      )

class TestBlockedChunksOf(unittest.TestCase, BlockifyTestMixin):
  ''' Run the tests against blocked_chunks_of.
  '''

  @staticmethod
  def BLOCKED(*a, **kw):
    ''' Call `blocked_chunks_of` for this subclass.
    '''
    return blocked_chunks_of(*a, **kw)

class TestBlockedChunksOf2(unittest.TestCase, BlockifyTestMixin):
  ''' Run the tests against blocked_chunks_of2.
  '''

  @staticmethod
  def BLOCKED(*a, **kw):
    ''' Call `blocked_chunks_of2` for this subclass.
    '''
    return blocked_chunks_of2(*a, **kw)

def selftest(argv):
  ''' Run the unit tests.
  '''
  unittest.main(__name__, None, argv)

if __name__ == '__main__':
  selftest(sys.argv)
