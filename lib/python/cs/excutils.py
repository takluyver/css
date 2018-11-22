#!/usr/bin/python -tt
#
# Convenience facilities for exceptions.
#       - Cameron Simpson <cs@cskk.id.au>
#

r'''
Convenience facilities for managing exceptions.

Presents:

* return_exc_info: call supplied function with arguments, return either (function_result, None) or (None, exc_info) if an exception was raised.

* @returns_exc_info, a decorator for a function which wraps in it return_exc_info.

* @noexc, a decorator for a function whose exceptions should never escape; instead they are logged. The initial use case was inside logging functions, where I have had a failed logging action abort a program. Obviously this is a decorator which should see very little use.

* @noexc_gen, a decorator for generators with similar effect to @noexc for ordinary functions.

* NoExceptions, a context manager to intercept most exceptions

* LogExceptions, a context manager to log exceptions

* @logexc, a decorator to make a function log exceptions it raises

* @transmute, a decorator to transmute an inner exception to another exception type

* @unattributable, a decorator to transmute inner AttributeError into a RuntimeError

* @unimplemented, a decorator to make a method raise NotImplementedError
'''

import sys
import traceback

DISTINFO = {
    'description': "Convenience facilities for managing exceptions.",
    'keywords': ["python2", "python3"],
    'classifiers': [
        "Programming Language :: Python",
        "Programming Language :: Python :: 2",
        "Programming Language :: Python :: 3",
    ],
    'install_requires': [],
}

def return_exc_info(func, *args, **kwargs):
  ''' Run the supplied function and arguments.
      Return:
        func_return, None
      in the case of successful operation and:
        None, exc_info
      in the case of an exception. `exc_info` is a 3-tuple of
      exc_type, exc_value, exc_traceback as returned by sys.exc_info().
      If you need to protect a whole suite and would rather not move it
      into its own function, consider the NoExceptions context manager.
  '''
  try:
    result = func(*args, **kwargs)
  except Exception:
    return None, tuple(sys.exc_info())
  return result, None

def returns_exc_info(func):
  ''' Decorator function to wrap functions whose exceptions should be caught,
      such as inside event loops or worker threads.
      It causes a function to return:
        func_return, None
      in the case of successful operation and:
        None, exc_info
      in the case of an exception. `exc_info` is a 3-tuple of
      exc_type, exc_value, exc_traceback as returned by sys.exc_info().
  '''
  def returns_exc_info_wrapper(*args, **kwargs):
    return return_exc_info(func, *args, **kwargs)
  return returns_exc_info_wrapper

def noexc(func):
  ''' Decorator to wrap a function which should never raise an exception.
      Instead, any raised exception is attempted to be logged.
      A significant side effect is of course that if the function raises an
      exception it now returns None.
      My primary use case is actually to wrap logging functions,
      which I have had abort otherwise sensible code.
  '''
  def noexc_wrapper(*args, **kwargs):
    from cs.logutils import exception
    from cs.x import X
    try:
      return func(*args, **kwargs)
    except Exception as e:
      try:
        exception(
            "exception calling %s(%s, **(%s))", func.__name__, args, kwargs)
      except Exception as e:
        try:
          X("exception calling %s(%s, **(%s)): %s",
            func.__name__, args, kwargs, e)
        except Exception:
          pass
  noexc_wrapper.__name__ = 'noexc(%s)' % (func.__name__,)
  return noexc_wrapper

def noexc_gen(func):
  ''' Decorator to wrap a generator which should never raise an exception.
      Instead, any raised exception is attempted to be logged and iteration ends.
      My primary use case is wrapping generators chained in a pipeline,
      as in cs.later.Later.pipeline.
  '''
  from cs.logutils import exception
  from cs.x import X

  def noexc_gen_wrapper(*args, **kwargs):
    try:
      it = iter(func(*args, **kwargs))
    except Exception as e0:
      try:
        exception(
            "exception calling %s(*%s, **(%s)): %s", func.__name__, args, kwargs, e0)
      except Exception as e2:
        try:
          X("exception calling %s(*%s, **(%s)): %s",
            func.__name__, args, kwargs, e2)
        except Exception:
          pass
      return
    while True:
      try:
        item = next(it)
      except StopIteration:
        raise
      except Exception as e:
        try:
          exception("exception calling next(%s(*%s, **(%s))): %s",
                    func.__name__, args, kwargs, e)
        except Exception as e2:
          try:
            X("exception calling next(%s(*%s, **(%s))): %s",
              func.__name__, args, kwargs, e)
          except Exception:
            pass
        return
      else:
        yield item
  noexc_gen_wrapper.__name__ = 'noexc_gen(%s)' % (func.__name__,)
  return noexc_gen_wrapper

def transmute(exc_from, exc_to=None):
  ''' Decorator to transmute an inner exception to another exception type.
      The motivating use case is properties in a class with a
      __getattr__ method; if some inner operation of the property
      function raises AttributeError then the property is bypassed
      in favour of __getattr__. Confusion ensues.
      In principle this can be an issue with any exception raised
      from "deeper" in the call chain, which can be mistaken for a
      "shallow" exception raise by the function itself.
  '''
  if exc_to is None:
    exc_to = RuntimeError

  def transmutor(func):
    def transmute_transmutor_wrapper(*a, **kw):
      try:
        return func(*a, **kw)
      except exc_from as e:
        raise exc_to("inner %s:%s transmuted to %s" % (type(e), e, exc_to))
    return transmute_transmutor_wrapper
  return transmutor

def unattributable(func):
  return transmute(AttributeError, RuntimeError)(func)

def safe_property(func):
  return property(unattributable(func))

def unimplemented(func):
  ''' Decorator for stub methods that must be implemented by a stub class.
  '''

  def unimplemented_wrapper(self, *a, **kw):
    raise NotImplementedError(
        "%s.%s(*%s, **%s)" % (type(self), func.__name__, a, kw))
  return unimplemented_wrapper

class NoExceptions(object):

  ''' A context manager to catch _all_ exceptions and log them.
      Arguably this should be a bare try...except but that's syntacticly
      noisy and separates the catch from the top.
      For simple function calls return_exc_info() is probably better.
  '''

  def __init__(self, handler):
    ''' Initialise the NoExceptions context manager.
        The handler is a callable which
        expects (exc_type, exc_value, traceback)
        and returns True or False for the __exit__
        method of the manager.
        If handler is None, the __exit__ method
        always returns True, suppressing any exception.
    '''
    self.handler = handler

  def __enter__(self):
    pass

  def __exit__(self, exc_type, exc_value, tb):
    if exc_type is not None:
      if self.handler is not None:
        return self.handler(exc_type, exc_value, tb)
      # report handled exception
      from cs.logutils import warning
      warning("IGNORE  " + str(exc_type) + ": " + str(exc_value))
      for line in traceback.format_tb(tb):
        warning("IGNORE> " + line[:-1])
    return True

def LogExceptions(conceal=False):
  ''' Wrapper of NoExceptions which reports exceptions and optionally
      suppresses them.
  '''
  from cs.logutils import exception
  def handler(exc_type, exc_value, exc_tb):
    exception("EXCEPTION: <%s> %s", exc_type, exc_value)
    for line in traceback.format_exception(exc_type, exc_value, exc_tb):
      exception("EXCEPTION> " + line)
    return conceal
  return NoExceptions(handler)

def logexc(func):
  def logexc_wrapper(*a, **kw):
    with LogExceptions():
      return func(*a, **kw)
  try:
    name = func.__name__
  except AttributeError:
    name = str(func)
  logexc_wrapper.__name__ = 'logexc(%s)' % (name,)
  return logexc_wrapper

def logexc_gen(genfunc):
  def logexc_gen_wrapper(*a, **kw):
    with LogExceptions():
      it = genfunc(*a, **kw)
      while True:
        try:
          item = next(it)
        except StopIteration:
          return
        yield item
  logexc_gen_wrapper.__name__ = 'logexc_gen(%s)' % (genfunc.__name__,)
  return logexc_gen_wrapper

def try_LogExceptions(e, conceal):
  # optionally fire off an exception, used in testing
  with LogExceptions(conceal=conceal):
    if e:
      raise e

def try_logexc(e):
  # optionally fire off an exception, used in testing
  @logexc
  def f(e):
    if e:
      raise e
  f(e)

if __name__ == '__main__':
  import cs.excutils_tests
  cs.excutils_tests.selftest(sys.argv)
