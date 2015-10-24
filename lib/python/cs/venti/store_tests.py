#!/usr/bin/python
#
# Self tests for cs.venti.store.
#       - Cameron Simpson <cs@zip.com.au>
#

import os
from os.path import abspath
import random
import shutil
import sys
import tempfile
import unittest
from cs.excutils import logexc
from cs.logutils import setup_logging, warning, X
from cs.randutils import rand0, randblock
from . import _TestAdditionsMixin
from .datafile import GDBMIndex, KyotoIndex
from .store import MappingStore, DataDirStore
from .hash import HashUtilDict
from .hash_tests import _TestHashCodeUtils

class _TestStore(_TestAdditionsMixin):

  def setUp(self):
    self._init_Store()
    self.S.open()

  def _init_Store(self):
    raise unittest.SkipTest("no Store in base class")

  def tearDown(self):
    self.S.close()

  def test00empty(self):
    S = self.S
    self.assertLen(S, 0)
    S.flush()

  def test01add_new_block(self):
    S = self.S
    self.assertLen(S, 0)
    size = random.randint(127, 16384)
    data = randblock(size)
    # compute blakc hash but do not store
    h = S.hash(data)
    self.assertLen(S, 0)
    ok = S.contains(h)
    self.assertFalse(ok)
    self.assertNotIn(h, S)
    # now add the block
    h2 = S.add(data)
    self.assertEqual(h, h2)
    self.assertLen(S, 1)
    ok = S.contains(h)
    self.assertTrue(ok)
    self.assertIn(h, S)
    S.flush()

  def test02add_get(self):
    S = self.S
    self.assertLen(S, 0)
    random_chunk_map = {}
    for _ in range(16):
      size = random.randint(127, 16384)
      data = randblock(size)
      h = S.hash(data)
      h2 = S.add(data)
      self.assertEqual(h, h2)
      random_chunk_map[h] = data
    self.assertLen(S, 16)
    for h in random_chunk_map:
      chunk = S.get(h)
      self.assertIsNot(chunk, None)
      self.assertEqual(chunk, random_chunk_map[h])
    S.flush()

  def test03first(self):
    S = self.S
    self.assertLen(S, 0)
    try:
      first_hashcode = S.first()
    except NotImplementedError as e:
      raise unittest.SkipTest("no .first in %s: %s" % (S, e))
    else:
      self.assertIs(first_hashcode, None, ".first of empty Store should be None")
    random_chunk_map = {}
    for _ in range(16):
      size = random.randint(127, 16384)
      data = randblock(size)
      h = S.hash(data)
      h2 = S.add(data)
      self.assertEqual(h, h2)
      random_chunk_map[h] = data
    self.assertLen(S, 16)
    ordered_hashcodes = sorted(random_chunk_map.keys())
    first_hashcode = S.first()
    self.assertIsNot(first_hashcode, None, ".first of nonempty Store should not be None")
    self.assertEqual(first_hashcode, ordered_hashcodes[0])

class TestMappingStore(_TestStore, unittest.TestCase):

  def _init_Store(self):
    self.S = MappingStore({}).open()

class TestHashCodeUtilsMappingStoreDict(_TestHashCodeUtils, unittest.TestCase):
  ''' Test HashUtils on a MappingStore on a plain dict.
  '''
  MAP_FACTORY = lambda self: MappingStore({})

class TestHashCodeUtilsMappingStoreHashUtilDict(_TestHashCodeUtils, unittest.TestCase):
  ''' Test HashUtils on a MappingStore on a HashUtilDict.
  '''
  MAP_FACTORY = lambda self: MappingStore(HashUtilDict())

class _TestDataDirStore(_TestStore):

  INDEX_CLASS = None

  def _init_Store(self):
    indexclass = self.__class__.INDEX_CLASS
    random.seed()
    self.pathname = self.mktmpdir()
    self.S = DataDirStore(self.pathname, indexclass=indexclass, rollover=200000)

  def tearDown(self):
    ##os.system("ls -l "+self.pathname)
    shutil.rmtree(self.pathname)
    _TestStore.tearDown(self)

class TestDataDirStoreGDBM(_TestDataDirStore, unittest.TestCase):
  INDEX_CLASS = GDBMIndex

class TestHashCodeUtilsDataDirStoreGDBMStore(_TestHashCodeUtils, unittest.TestCase):
  MAP_FACTORY = lambda self: DataDirStore(self.mktmpdir(), indexclass=GDBMIndex, rollover=200000)

class TestDataDirStoreKyoto(_TestDataDirStore, unittest.TestCase):
  INDEX_CLASS = KyotoIndex

class TestHashCodeUtilsDataDirStoreKyotoStore(_TestHashCodeUtils, unittest.TestCase):
  MAP_FACTORY = lambda self: DataDirStore(self.mktmpdir(), indexclass=KyotoIndex, rollover=200000)

def selftest(argv):
  unittest.main(__name__, None, argv)

if __name__ == '__main__':
  setup_logging(__file__)
  selftest(sys.argv)
