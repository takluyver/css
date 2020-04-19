#!/usr/bin/python
#
# Single line status updates.
#   - Cameron Simpson <cs@cskk.id.au>

r'''
Single line status updates with minimal update sequences.

This is available as an output mode in `cs.logutils`.

Example:

    with Upd() as U:
        for filename in filenames:
            U.out(filename)
            ... process filename ...
            upd.nl('an informational line')
'''

from __future__ import with_statement
import atexit
from contextlib import contextmanager
from threading import RLock
from cs.gimmicks import warning
from cs.lex import unctrl
from cs.obj import SingletonMixin
from cs.tty import ttysize

try:
  import curses
except ImportError as e:
  warning("cannot import curses: %s", e)
  curses = None

__version__ = '20200229'

DISTINFO = {
    'keywords': ["python2", "python3"],
    'classifiers': [
        "Programming Language :: Python",
        "Programming Language :: Python :: 2",
        "Programming Language :: Python :: 3",
    ],
    'install_requires': ['cs.gimmicks', 'cs.lex', 'cs.obj', 'cs.tty'],
}

instances = []

def cleanupAtExit():
  ''' Cleanup function called at programme exit to clear the status line.
  '''
  global instances
  for i in instances:
    i.close()
  instances = ()

atexit.register(cleanupAtExit)

class Upd(SingletonMixin):
  ''' A `SingletonMixin` subclass for maintaining a regularly updated status line.
  '''

  @classmethod
  def _singleton_key(cls, backend, columns=None):
    return id(backend)

  def _singleton_init(self, backend, columns=None):
    assert backend is not None
    if columns is None:
      columns = 80
      if backend.isatty():
        rc = ttysize(backend)
        if rc.columns is not None:
          columns = rc.columns
    self._backend = backend
    self.columns = columns
    self._ti_ready = False
    self._ti_strs = {}
    self._slot_text = ['']
    self._current_slot = 0
    self._above = None
    self._lock = RLock()
    global instances
    instances.append(self)

  def __enter__(self):
    return self

  def __exit__(self, exc_type, *_):
    ''' Tidy up on exiting the context.

        If we are exiting because of an exception and the status
        line is not empty, output a newline to preserve the status
        line on the screen.  Otherwise just clear the status line.
    '''
    if self._slot_text[0]:
      if exc_type:
        self._backend.write('\n')
        self._backend.flush()
      else:
        self.out('')

  def close(self):
    ''' Close this Upd.
    '''
    if self._backend is not None:
      self.out('')
      self._backend = None

  def closed(self):
    ''' Test whether this Upd is closed.
    '''
    return self._backend is None

  def ti_str(self, ti_name):
    ''' Fetch the terminfo capability string named `ti_name`.
        Return the string or `None` if not available.
    '''
    try:
      return self._ti_strs[ti_name]
    except KeyError:
      with self._lock:
        if curses is None:
          s = None
        else:
          if not self._ti_ready:
            curses.setupterm()
            self._ti_ready = True
          s = curses.tigetstr(ti_name)
        self._ti_strs[ti_name] = s
      return s

  @staticmethod
  def adjust_text_v(oldtxt, newtxt, columns, raw_text=False):
    ''' Compute the text sequences required to update `oldtxt` to `newtxt`
        presuming the cursor is at the right hand end of `oldtxt`.
        The available area is specified by `columns`.

        We normalise `newtxt` as `unctrl(newtxt.rstrip())`.
        `oldtxt` is presumed to be already normalised.
    '''
    # normalise text
    if not raw_text:
      newtxt = unctrl(newtxt.rstrip())
    # crop for terminal width
    newlen = len(newtxt)
    if newlen >= columns:
      newtxt = newtxt[:columns - 1]
      newlen = len(newtxt)
    oldlen = len(oldtxt)
    pfxlen = min(newlen, oldlen)
    # compute length of common prefix
    for i in range(pfxlen):
      if newtxt[i] != oldtxt[i]:
        pfxlen = i
        break
    # Rewrites take one of two forms:
    #   Backspace to end of common prefix, overwrite with the differing tail
    #     of the new string, erase trailing extent if any.
    #   Return to start of line with carriage return, overwrite with new
    #    string, erase trailing extent if any.
    # Therefore compare backspaces against cr+pfxlen.
    #
    if oldlen - pfxlen < 1 + pfxlen:
      # backspace and partial overwrite
      difftxts = ['\b' * (oldlen - pfxlen), newtxt[pfxlen:]]
    else:
      # carriage return and complete overwrite
      difftxts = ['\r', newtxt]
    # trailing text to overwrite with spaces?
    extlen = oldlen - newlen
    if extlen > 0:
      # old line was longer - write spaces over the old tail
      difftxts.append(' ' * extlen)
      difftxts.append('\b' * extlen)
    return difftxts

  def move_to_slot_v(self, from_slot, to_slot):
    ''' Compute the text sequences required to move our cursor
        to the end of `to_slot` from `from_slot`.
    '''
    assert from_slot >= 0
    assert to_slot >= 0
    if from_slot is None:
      from_slot = self._current_slot
    movetxts = []
    oldtxt = self._slot_text[to_slot]
    from_slot = self._current_slot
    if to_slot != from_slot:
      # move cursor to end of target slot
      if to_slot < from_slot:
        # emit VT
        movetxts.append('\v' * (from_slot - to_slot))
      else:
        # emit cursor_up
        cuu1 = self.ti_str('cuu1')
        movetxts.append(cuu1 * (from_slot - to_slot))
      # adjust horizontal position
      vpos_cur = len(self._slot_text[from_slot])
      vpos_slot = len(oldtxt)
      if vpos_cur > vpos_slot:
        # backspace
        movetxts.append('\b' * (vpos_cur - vpos_slot))
      elif vpos_cur < vpos_slot:
        # overwrite to advance cursor
        movetxts.append(oldtxt[vpos_cur:])
    return movetxts

  def out(self, txt, *a, slot=0, raw_text=False):
    ''' Update the status line to `txt`.
        Return the previous status line content.

        Parameters:
        * `txt`: the status line text.
        * `a`: optional positional parameters;
          if not empty, `txt` is percent formatted against this list.
        * `slot`: which slot to update; default is `0`, the bottom slot
        * `raw_text`: if true (default `False`), do not normalise the text
    '''
    if a:
      txt = txt % a
    if not raw_text:
      txt = unctrl(txt.rstrip())
    backend = self._backend
    with self._lock:
      current_slot = self._current_slot
      oldtxt = self._slot_text[current_slot]
      if oldtxt != txt:
        # move to target slot and collect reference text
        txts = self.move_to_slot_v(current_slot, slot)
        # now adjust slot display
        txts.extend(
            self.adjust_text_v(
                oldtxt, txt, self.columns, raw_text=True
            )
        )
        backend.write(''.join(txts))
        backend.flush()
        self._current_slot = slot
        self._slot_text[slot] = txt
    return txt

  def nl(self, txt, *a, raw=False):
    ''' Write `txt` to the backend followed by a newline.

        Parameters:
        * `txt`: the message to write.
        * `a`: optional positional parameters;
          if not empty, `txt` is percent formatted against this list.
        * `raw`: if true (default `False`) use the "clear, newline,
          restore" method.

        This uses one of two methods:
        * insert above:
          insert a line above the status line and write the message there.
        * clear, newline, restore:
          clears the status line, writes the text line, restores
          the status line.

        The former method is used if the terminal supports the
        `il1` (insert one line) capability;
        this is probed for on the first use and remembered.
    '''
    if a:
      txt = txt % a
    if raw or len(txt) >= self.columns:
      # force a clear-newline-restore method
      above = False
    else:
      # try to insert the output above the status line
      above = self._above
      if above is None:
        il1 = self.ti_str('il1')
        if il1:
          above = ((il1 + b'\r').decode(), '\n')
        else:
          above = False
        self._above = above
    if above:
      with self._lock:
        # TODO:
        self._backend.write(above[0] + txt + above[1] + self._slot_text[0])
        self._backend.flush()
    else:
      with self.without():
        with self._lock:
          self._backend.write(txt + '\n')

  def flush(self):
    ''' Flush the output stream.
    '''
    if self._backend:
      self._backend.flush()

  @contextmanager
  def without(self, temp_state=''):
    ''' Context manager to clear the status line around a suite.
        Returns the status line text as it was outside the suite.

        The `temp_state` parameter may be used to set the inner status line
        content if a value other than `''` is desired.
    '''
    with self._lock:
      old = self.out(temp_state)
      try:
        yield old
      finally:
        self.out(old)
