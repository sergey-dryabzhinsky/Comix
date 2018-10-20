"""archive.py - Archive handling (extract/create) for Comix."""

import sys
import os
import re
import zipfile
import tarfile
import threading
import cStringIO
try:
    from py7zlib import Archive7z
except ImportError:
    Archive7z = None  # ignore it.
import mobiunpack

import gtk

import process
from image import get_supported_format_extensions_preg

ZIP, RAR, TAR, GZIP, BZIP2, SEVENZIP, MOBI, DIRECTORY = range(8)

_rar_exec = None
_7z_exec = None

class Extractor:

    """Extractor is a threaded class for extracting different archive formats.

    The Extractor can be loaded with paths to archives (currently ZIP, tar,
    or RAR archives) and a path to a destination directory. Once an archive
    has been set it is possible to filter out the files to be extracted and
    set the order in which they should be extracted. The extraction can
    then be started in a new thread in which files are extracted one by one,
    and a signal is sent on a condition after each extraction, so that it is
    possible for other threads to wait on specific files to be ready.

    Note: Support for gzip/bzip2 compressed tar archives is limited, see
    set_files() for more info.
    """

    def __init__(self):
        self._setupped = False

    def setup(self, src, dst):
        """Setup the extractor with archive <src> and destination dir <dst>.
        Return a threading.Condition related to the is_ready() method, or
        None if the format of <src> isn't supported.
        """
        self._src = src
        self._dst = dst
        self._type = archive_mime_type(src)
        self._files = []
        self._extracted = {}
        self._stop = False
        self._extract_thread = None
        self._condition = threading.Condition()

        if self._type == ZIP:
            self._zfile = zipfile.ZipFile(src, 'r')
            self._files = self._zfile.namelist()
        elif self._type in (TAR, GZIP, BZIP2):
            self._tfile = tarfile.open(src, 'r')
            self._files = self._tfile.getnames()
        elif self._type == RAR:
            global _rar_exec
            if _rar_exec is None:
                _rar_exec = _get_rar_exec()
                if _rar_exec is None:
                    print( '! Could not find RAR file extractor.')
                    dialog = gtk.MessageDialog(None, 0, gtk.MESSAGE_WARNING,
                        gtk.BUTTONS_CLOSE,
                        _("Could not find RAR file extractor!"))
                    dialog.format_secondary_markup(
                        _("You need either the <i>rar</i> or the <i>unrar</i> program installed in order to read RAR (.cbr) files."))
                    dialog.run()
                    dialog.destroy()
                    return None
            proc = process.Process([_rar_exec, 'vb', '-p-', '--', src])
            fd = proc.spawn()
            self._files = [name.rstrip(os.linesep) for name in fd.readlines()]
            fd.close()
            proc.wait()
        elif self._type == SEVENZIP:
            global _7z_exec, Archive7z

            if not Archive7z:  # lib import failed
                print( ': pylzma is not installed... will try 7z tool...')

                if _7z_exec is None:
                    _7z_exec = _get_7z_exec()
            else:
                try:
                    self._szfile = Archive7z(open(src,'rb'),'-')
                    self._files = self._szfile.getnames()
                except:
                    Archive7z = None
                    # pylzma can fail on new 7z
                    if _7z_exec is None:
                        _7z_exec = _get_7z_exec()

            if _7z_exec is None:
                print('! Could not find 7Z file extractor.')
            elif not Archive7z:
                proc = process.Process([_7z_exec, 'l', '-bd', '-slt', '-p-', src])
                fd = proc.spawn()
                self._files = self._process_7z_names(fd)
                fd.close()
                proc.wait()

            if not _7z_exec and not Archive7z:
                dialog = gtk.MessageDialog(None, 0, gtk.MESSAGE_WARNING,
                    gtk.BUTTONS_CLOSE,
                    _("Could not find 7Z file extractor!"))
                dialog.format_secondary_markup(
                    _("You need either the <i>pylzma</i> or the <i>p7zip</i> program installed in order to read 7Z (.cb7) files."))
                dialog.run()
                dialog.destroy()
                return None
        elif self._type == MOBI:
            self._mobifile = None
            try:
                self._mobifile = mobiunpack.MobiFile(src)
                self._files = self._mobifile.getnames()
            except mobiunpack.unpackException as e:
                print('! Failed to unpack MobiPocket:', e)
                return None

        elif self._type == DIRECTORY:
            for r,d,f in os.walk(src):
                for _f in f:
                    self._files.append(_f)
                    self.extracted[_f] = True
            pass
        else:
            print('! Non-supported archive format:', src)
            return None

        self._setupped = True
        return self._condition

    def _process_7z_names(self, fd):
        START = "----------"
        names = []
        started = False
        item = {}

        while True:

            try:
                line = fd.readline()
            except:
                break

            if line:
                line = line.rstrip(os.linesep)
                try:
                    # For non-ascii files names
                    line = line.decode("utf-8")
                except:
                    pass

                if line.startswith(START):
                    started = True
                    item = {}
                    continue

                if started:
                    if line == "":
                        if item["Attributes"].find("D") == -1:
                            names.append(item["Path"])
                        item = {}
                    else:
                        key = line.split("=")[0].strip()
                        value = "=".join(line.split("=")[1:]).strip()
                        item[key] = value
            else:
                break

        return names


    def get_files(self):
        """Return a list of names of all the files the extractor is currently
        set for extracting. After a call to setup() this is by default all
        files found in the archive. The paths in the list are relative to
        the archive root and are not absolute for the files once extracted.
        """
        return self._files[:]

    def set_files(self, files, extracted=False):
        """Set the files that the extractor should extract from the archive in
        the order of extraction. Normally one would get the list of all files
        in the archive using get_files(), then filter and/or permute this
        list before sending it back using set_files().

        The second parameter, extracted allows a trick for the subarchive
        managing : setting files as extracted, in order to avoid any blocking
        wait on files not present in the original archive.

        Note: Random access on gzip or bzip2 compressed tar archives is
        no good idea. These formats are supported *only* for backwards
        compability. They are fine formats for some purposes, but should
        not be used for scanned comic books. So, we cheat and ignore the
        ordering applied with this method on such archives.
        """
        if extracted:
            self._files = files
            for file in files:
                self._extracted[file] = True
            return
        if self._type in (GZIP, BZIP2):
            self._files = [x for x in self._files if x in files]
        else:
            self._files = files

    def is_ready(self, name):
        """Return True if the file <name> in the extractor's file list
        (as set by set_files()) is fully extracted.
        """
        return self._extracted.get(name, False)

    def get_mime_type(self):
        """Return the mime type name of the extractor's current archive."""
        return self._type

    def stop(self):
        """Signal the extractor to stop extracting and kill the extracting
        thread. Blocks until the extracting thread has terminated.
        """
        self._stop = True
        if self._setupped:
            self._extract_thread.join()
            self.setupped = False

    def extract(self):
        """Start extracting the files in the file list one by one using a
        new thread. Every time a new file is extracted a notify() will be
        signalled on the Condition that was returned by setup().
        """
        self._extract_thread = threading.Thread(target=self._thread_extract)
        self._extract_thread.setDaemon(False)
        self._extract_thread.start()

    def close(self):
        """Close any open file objects, need only be called manually if the
        extract() method isn't called.
        """
        if self._type == ZIP:
            self._zfile.close()
        elif self._type in (TAR, GZIP, BZIP2):
            self._tfile.close()
        elif self._type == MOBI and self._mobifile is not None:
            self._mobifile.close()

    def _thread_extract(self):
        """Extract the files in the file list one by one."""
        # Extract 7z and rar whole archive - if it SOLID - extract one file is SLOW
        if self._type in (SEVENZIP,) and _7z_exec is not None:
            cmd = [_7z_exec, 'x', '-bd', '-p-',
                '-o'+self._dst, '-y', self._src]
            proc = process.Process(cmd)
            proc.spawn()
            proc.wait()
            self._condition.acquire()
            for name in self._files:
                self._extracted[name] = True
            self._condition.notify()
            self._condition.release()
        if self._type in (RAR,) and _rar_exec is not None:
            cwd = os.getcwd()
            os.chdir(self._dst)
            cmd = [_rar_exec, 'x', '-kb', '-p-',
                        '-o-', '-inul', '--', self._src]
            proc = process.Process(cmd)
            proc.spawn()
            proc.wait()
            os.chdir(cwd)
            self._condition.acquire()
            for name in self._files:
                self._extracted[name] = True
            self._condition.notify()
            self._condition.release()
        else:
            for name in self._files:
                self._extract_file(name)
        self.close()

    def _extract_file(self, name):
        """Extract the file named <name> to the destination directory,
        mark the file as "ready", then signal a notify() on the Condition
        returned by setup().
        """
        if self._stop:
            self.close()
            sys.exit(0)
        try:
            if self._type in (ZIP, SEVENZIP):
                dst_path = os.path.join(self._dst, name)
                if not os.path.exists(os.path.dirname(dst_path)):
                    os.makedirs(os.path.dirname(dst_path))
                new = open(dst_path, 'wb')
                if self._type == ZIP:
                    new.write(self._zfile.read(name, '-'))
                elif self._type == SEVENZIP:
                    if Archive7z is not None:
                        new.write(self._szfile.getmember(name).read())
                    else:
                        if _7z_exec is not None:
                            proc = process.Process([_7z_exec, 'x', '-bd', '-p-',
                                '-o'+self._dst, '-y', self._src, name])
                            proc.spawn()
                            proc.wait()
                        else:
                            print '! Could not find 7Z file extractor.'

                new.close()
            elif self._type in (TAR, GZIP, BZIP2):
                if os.path.normpath(os.path.join(self._dst, name)).startswith(
                  self._dst):
                    self._tfile.extract(name, self._dst)
                else:
                    print '! Non-local tar member:', name, '\n'
            elif self._type == RAR:
                if _rar_exec is not None:
                    cwd = os.getcwd()
                    os.chdir(self._dst)
                    proc = process.Process([_rar_exec, 'x', '-kb', '-p-',
                        '-o-', '-inul', '--', self._src, name])
                    proc.spawn()
                    proc.wait()
                    os.chdir(cwd)
                else:
                    print '! Could not find RAR file extractor.'
            elif self._type == MOBI:
                dst_path = os.path.join(self._dst, name)
                self._mobifile.extract(name, dst_path)
        except Exception:
            # Better to ignore any failed extractions (e.g. from a corrupt
            # archive) than to crash here and leave the main thread in a
            # possible infinite block. Damaged or missing files *should* be
            # handled gracefully by the main program anyway.
            pass
        self._condition.acquire()
        self._extracted[name] = True
        self._condition.notify()
        self._condition.release()

    def extract_file_io(self, chosen):
        """Extract the file named <name> to the destination directory,
        mark the file as "ready", then signal a notify() on the Condition
        returned by setup().
        """

        if os.path.exists(os.path.join(self._dst, chosen)):
            return cStringIO.StringIO(open(os.path.join(self._dst, chosen), 'rb').read())

        if self._type == DIRECTORY:
            return cStringIO.StringIO(open(os.path.join(self._src, chosen), 'rb').read())
        if self._type == ZIP:
            return cStringIO.StringIO(self._zfile.read(chosen))
        elif self._type in [TAR, GZIP, BZIP2]:
            return cStringIO.StringIO(self._tfile.extractfile(chosen).read())
        elif self._type == RAR:
            proc = process.Process([_rar_exec, 'p', '-inul', '-p-', '--',
                self._src, chosen])
            fobj = proc.spawn()
            return cStringIO.StringIO(fobj.read())
        elif self._type == SEVENZIP:
            if Archive7z is not None:
                return cStringIO.StringIO(self._szfile.getmember(chosen).read())
            elif _7z_exec is not None:
                proc = process.Process([_7z_exec, 'e', '-bd', '-p-', '-so',
                    self._src, chosen])
                fobj = proc.spawn()
                return cStringIO.StringIO(fobj.read())


class Packer:

    """Packer is a threaded class for packing files into ZIP archives.

    It would be straight-forward to add support for more archive types,
    but basically all other types are less well fitted for this particular
    task than ZIP archives are (yes, really).
    """

    def __init__(self, image_files, other_files, archive_path, base_name):
        """Setup a Packer object to create a ZIP archive at <archive_path>.
        All files pointed to by paths in the sequences <image_files> and
        <other_files> will be included in the archive when packed.

        The files in <image_files> will be renamed on the form
        "NN - <base_name>.ext", so that the lexical ordering of their
        filenames match that of their order in the list.

        The files in <other_files> will be included as they are,
        assuming their filenames does not clash with other filenames in
        the archive. All files are placed in the archive root.
        """
        self._image_files = image_files
        self._other_files = other_files
        self._archive_path = archive_path
        self._base_name = base_name
        self._pack_thread = None
        self._packing_successful = False

    def pack(self):
        """Pack all the files in the file lists into the archive."""
        self._pack_thread = threading.Thread(target=self._thread_pack)
        self._pack_thread.setDaemon(False)
        self._pack_thread.start()

    def wait(self):
        """Block until the packer thread has finished. Return True if the
        packer finished its work successfully.
        """
        if self._pack_thread != None:
            self._pack_thread.join()
        return self._packing_successful

    def _thread_pack(self):
        try:
            zfile = zipfile.ZipFile(self._archive_path, 'w')
        except Exception:
            print '! Could not create archive', self._archive_path
            return
        used_names = []
        pattern = '%%0%dd - %s%%s' % (len(str(len(self._image_files))),
            self._base_name)
        for i, path in enumerate(self._image_files):
            filename = pattern % (i + 1, os.path.splitext(path)[1])
            try:
                zfile.write(path, filename, zipfile.ZIP_STORED)
            except Exception:
                print '! Could not add file %s to add to %s, aborting...' % (
                    path, self._archive_path)
                zfile.close()
                try:
                    os.remove(self._archive_path)
                except:
                    pass
                return
            used_names.append(filename)
        for path in self._other_files:
            filename = os.path.basename(path)
            while filename in used_names:
                filename = '_%s' % filename
            try:
                zfile.write(path, filename, zipfile.ZIP_DEFLATED)
            except Exception:
                print '! Could not add file %s to add to %s, aborting...' % (
                    path, self._archive_path)
                zfile.close()
                try:
                    os.remove(self._archive_path)
                except:
                    pass
                return
            used_names.append(filename)
        zfile.close()
        self._packing_successful = True


def archive_mime_type(path):
    """Return the archive type of <path> or None for non-archives."""
    try:
        if os.path.isfile(path):
            if not os.access(path, os.R_OK):
                return None
            if zipfile.is_zipfile(path):
                return ZIP
            fd = open(path, 'rb')
            magic = fd.read(4)
            fd.seek(60)
            magic2 = fd.read(8)
            fd.close()
            if tarfile.is_tarfile(path) and os.path.getsize(path) > 0:
                if magic.startswith('BZh'):
                    return BZIP2
                if magic.startswith('\037\213'):
                    return GZIP
                return TAR
            if magic == 'Rar!':
                return RAR
            if magic == '7z\xbc\xaf':
                return SEVENZIP
            if magic2 == 'BOOKMOBI':
                return MOBI
        elif os.path.isdir(path):
            return DIRECTORY
    except Exception:
        print '! Error while reading', path
    return None


def get_name(archive_type):
    """Return a text representation of an archive type."""
    return {ZIP:   _('ZIP archive'),
            TAR:   _('Tar archive'),
            GZIP:  _('Gzip compressed tar archive'),
            BZIP2: _('Bzip2 compressed tar archive'),
            RAR:   _('RAR archive'),
            SEVENZIP: _('7-Zip archive'),
            MOBI:  _('MobiPocket file'),
            DIRECTORY:  _('Directory'),
           }[archive_type]


def get_archive_info(path):
    """Return a tuple (mime, num_pages, size) with info about the archive
    at <path>, or None if <path> doesn't point to a supported archive.
    """
    image_re = re.compile('\.('+'|'.join(get_supported_format_extensions_preg())+')\s*$', re.I)
    extractor = Extractor()
    extractor.setup(path, None)
    mime = extractor.get_mime_type()
    if mime is None:
        return None
    files = extractor.get_files()
    extractor.close()
    num_pages = len(filter(image_re.search, files))
    size = os.stat(path).st_size
    return (mime, num_pages, size)


def _get_rar_exec():
    """Return the name of the RAR file extractor executable, or None if
    no such executable is found.
    """
    for command in ('unrar', 'rar'):
        if process.Process([command]).spawn() is not None:
            return command
    return None

def _get_7z_exec():
    """Return the name of the RAR file extractor executable, or None if
    no such executable is found.
    """
    for command in ('7z', '7za', '7zr'):
        if process.Process([command]).spawn() is not None:
            return command
    return None
