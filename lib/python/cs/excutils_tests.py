#!/usr/bin/python
#
# Self tests for cs.excutils.
#       - Cameron Simpson <cs@zip.com.au>
#

import sys
import unittest
from cs.excutils import return_exc_info, returns_exc_info, noexc, \
        try_LogExceptions, try_logexc

class TestExcUtils(unittest.TestCase):

  def test_return_exc_info(self):
    def divfunc(a, b):
      return a // b
    retval, exc_info = return_exc_info(divfunc, 4, 2)
    self.assertEqual(retval, 2)
    self.assertTrue(exc_info is None)
    retval, exc_info = return_exc_info(divfunc, 4, 0)
    self.assertTrue(retval is None)
    self.assertTrue(exc_info[0] is ZeroDivisionError)

  def test_returns_exc_info(self):
    @returns_exc_info
    def divfunc(a, b):
      return a // b
    retval, exc_info = divfunc(4, 2)
    self.assertEqual(retval, 2)
    self.assertTrue(exc_info is None)
    retval, exc_info = divfunc(4, 0)
    self.assertTrue(retval is None)
    self.assertTrue(exc_info[0] is ZeroDivisionError)

  def test_noexc(self):
    def f(to_raise=None):
      if to_raise is not None:
        raise to_raise()
      return True
    self.assertIs(f(), True)
    self.assertRaises(Exception, f, Exception)
    f2 = noexc(f)
    self.assertIs(f2(), True)
    self.assertIs(f2(Exception), None)

  def test_LogExceptions(self):
    from cs.logutils import X, setup_logging
    setup_logging("test_LogExceptions")
    bang = RuntimeError("bang! testing LogException")
    try_LogExceptions(None, conceal=True)
    try_LogExceptions(None, conceal=False)
    try_LogExceptions(bang, conceal=True)
    self.assertRaises(RuntimeError, try_LogExceptions, bang, conceal=False)
    self.assertRaises(Exception, try_LogExceptions, bang, conceal=False)

  def test_logexc(self):
    from cs.logutils import X, setup_logging
    setup_logging("test_logexc")
    bang = RuntimeError("bang! testing @logexc")
    try_logexc(None)
    self.assertRaises(RuntimeError, try_logexc, bang)
    self.assertRaises(Exception, try_logexc, bang)

def selftest(argv):
  unittest.main(__name__, None, argv)

if __name__ == '__main__':
  selftest(sys.argv)
