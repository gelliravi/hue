#!/usr/bin/env python
# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Classes for a custom upload handler to stream into HDFS.

Note that since our middlewares inspect request.POST, we cannot inject a custom
handler into a specific view. Therefore we always use the HDFSfileUploadHandler,
which is triggered by a magic prefix ("HDFS") in the field name.

See http://docs.djangoproject.com/en/1.2/topics/http/file-uploads/
"""

import errno
import logging
import time

from django.core.files.uploadhandler import \
    FileUploadHandler, StopFutureHandlers, StopUpload
import hadoop.cluster
from hadoop.conf import UPLOAD_CHUNK_SIZE

UPLOAD_SUBDIR = 'hue-uploads'
LOG = logging.getLogger(__name__)


class HDFSerror(Exception):
  pass

class HDFStemporaryUploadedFile(object):
  """
  A temporary HDFS file to store upload data.
  This class does not have any file read methods.
  """
  def __init__(self, request, name, destination):
    self.name = name
    self.size = None
    self._do_cleanup = False
    try:
      self._fs = request.fs
    except AttributeError:
      self._fs = hadoop.cluster.get_hdfs()

    # Don't want to handle this upload if we don't have an HDFS
    if not self._fs:
      raise HDFSerror("No HDFS found")

    # We want to set the user to be the superuser. But any operation
    # in the fs needs a username, including the retrieval of the superuser.
    # So we first set it to the DEFAULT_USER to break this chicken-&-egg.
    self._fs.setuser(self._fs.DEFAULT_USER)
    self._fs.setuser(self._fs.superuser)

    self._path = self._fs.mkswap(name, suffix='tmp', basedir=destination)

    self._file = self._fs.open(self._path, 'w')
    self._do_cleanup = True

  def __del__(self):
    if self._do_cleanup:
      # Do not do cleanup here. It's hopeless. The self._fs threadlocal states
      # are going to be all wrong.
      LOG.error("Left-over upload file is not cleaned up: %s" % (self._path,))

  def get_temp_path(self):
    return self._path

  def finish_upload(self, size):
    try:
      self.size = size
      self.close()
    except Exception, ex:
      LOG.exception('Error uploading file to %s' % (self._path,))
      raise

  def remove(self):
    try:
      original = self._fs.setskiptrash(True)
      self._fs.remove(self._path)
      self._fs.setskiptrash(original)
      self._do_cleanup = False
    except IOError, ex:
      if ex.errno != errno.ENOENT:
        LOG.exception('Failed to remove temporary upload file "%s". '
                      'Please cleanup manually: %s' % (self._path, ex))

  def write(self, data):
    self._file.write(data)

  def flush(self):
    self._file.flush()

  def close(self):
    self._file.close()


class HDFSfileUploadHandler(FileUploadHandler):
  """
  Handle file upload by storing data in a temp HDFS file.

  This handler is triggered by any upload field whose name starts with
  "HDFS" (case insensitive).
  """
  def __init__(self, request):
    FileUploadHandler.__init__(self, request)
    self._file = None
    self._starttime = 0
    self._activated = False
    self._destination = request.GET.get('dest', None)
    # Need to directly modify FileUploadHandler.chunk_size
    FileUploadHandler.chunk_size = UPLOAD_CHUNK_SIZE.get()

  def new_file(self, field_name, file_name, *args, **kwargs):
    # Detect "HDFS" in the field name.
    # NOTE: The user is not authenticated at this point, and it's
    #       very difficult to do so because we handle upload before
    #       running the auth middleware.
    if field_name.upper().startswith('HDFS'):
      try:
        self._file = HDFStemporaryUploadedFile(self.request, file_name, self._destination)
      except (HDFSerror, IOError), ex:
        LOG.error("Not using HDFS upload handler: %s" % (ex,))
        return

      LOG.debug('Upload attempt to %s' % (self._file.get_temp_path(),))
      self._activated = True
      self._starttime = time.time()
      raise StopFutureHandlers()

  def receive_data_chunk(self, raw_data, start):
    if not self._activated:
      return raw_data

    try:
      self._file.write(raw_data)
      self._file.flush()
      return None
    except IOError:
      LOG.exception('Error storing upload data in temporary file "%s"' %
                    (self._file.get_temp_path(),))
      raise StopUpload()

  def file_complete(self, file_size):
    if not self._activated:
      return None

    try:
      self._file.finish_upload(file_size)
    except IOError:
      LOG.exception('Error closing uploaded temporary file "%s"' %
                    (self._file.get_temp_path(),))
      raise

    elapsed = time.time() - self._starttime
    LOG.debug('Uploaded %s bytes to HDFS in %s seconds' % (file_size, elapsed))
    return self._file
