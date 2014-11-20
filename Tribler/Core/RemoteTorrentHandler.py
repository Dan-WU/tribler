# Written by Niels Zeilemaker
# see LICENSE.txt for license information
#
# Handles the case where the user did a remote query and now selected one of the
# returned torrents for download.
import Queue
import logging
import os
import sys
import urllib
import shutil
from collections import deque
from abc import ABCMeta, abstractmethod
from binascii import hexlify
from time import time
from tempfile import mkstemp
from tarfile import TarFile
from traceback import print_exc

from twisted.internet import reactor
from twisted.internet.task import LoopingCall

from Tribler.dispersy.taskmanager import TaskManager
from Tribler.dispersy.util import call_on_reactor_thread

from Tribler.Core.TorrentDef import TorrentDef
from Tribler.Core.simpledefs import NTFY_TORRENTS, INFOHASH_LENGTH


TORRENT_OVERFLOW_CHECKING_INTERVAL = 30 * 60
LOW_PRIO_COLLECTING = 0
MAGNET_TIMEOUT = 5.0


class RemoteTorrentHandler(TaskManager):

    __single = None

    def __init__(self):
        RemoteTorrentHandler.__single = self

        super(RemoteTorrentHandler, self).__init__()
        self._logger = logging.getLogger(self.__class__.__name__)

        self._searchcommunity = None

        self.torrent_callbacks = {}
        self.metadata_callbacks = {}

        self.torrent_requesters = {}
        self.torrent_message_requesters = {}
        self.magnet_requesters = {}
        self.metadata_requester = None

        self.num_torrents = 0

        self.session = None
        self.dispersy = None
        self.max_num_torrents = 0
        self.tor_col_dir = None
        self.torrent_db = None

    def getInstance(*args, **kw):
        if RemoteTorrentHandler.__single is None:
            RemoteTorrentHandler(*args, **kw)
        return RemoteTorrentHandler.__single
    getInstance = staticmethod(getInstance)

    def delInstance(*args, **kw):
        RemoteTorrentHandler.__single = None
    delInstance = staticmethod(delInstance)

    def register(self, dispersy, session, max_num_torrents):
        self.session = session
        self.dispersy = dispersy
        self.max_num_torrents = max_num_torrents
        self.tor_col_dir = self.session.get_torrent_collecting_dir()

        self.torrent_db = None
        if self.session.get_megacache():
            self.torrent_db = session.open_dbhandler(NTFY_TORRENTS)
            self.__check_overflow()

        if session.get_dht_torrent_collecting():
            self.magnet_requesters[0] = MagnetRequester(self.session, self, 0)
            self.magnet_requesters[1] = MagnetRequester(self.session, self, 1)
        self.metadata_requester = TftpMetadataRequester(self.session, self)

    def shutdown(self):
        self.cancel_all_pending_tasks()

    @call_on_reactor_thread
    def set_max_num_torrents(self, max_num_torrents):
        self.max_num_torrents = max_num_torrents

    @call_on_reactor_thread
    def __check_overflow(self):
        def clean_until_done(num_delete, deletions_per_step):
            """
            Delete torrents in steps to avoid too much IO at once.
            """
            if num_delete > 0:
                to_remove = min(num_delete, deletions_per_step)
                num_delete -= to_remove
                self.torrent_db.freeSpace(to_remove)
                self.register_task(u"remote_torrent clean_until_done",
                                   reactor.callLater(5, clean_until_done, num_delete, deletions_per_step))

        def torrent_overflow_check():
            """
            Check if we have reached the collected torrent limit and throttle its collection if so.
            """
            self.num_torrents = self.torrent_db.getNumberCollectedTorrents()
            self._logger.debug(u"check overflow: current %d max %d", self.num_torrents, self.max_num_torrents)

            if self.num_torrents > self.max_num_torrents:
                num_delete = int(self.num_torrents - self.max_num_torrents * 0.95)
                deletions_per_step = max(25, num_delete / 180)
                clean_until_done(num_delete, deletions_per_step)
                self._logger.info(u"** limit space:: %d %d %d", self.num_torrents, self.max_num_torrents, num_delete)

        self.register_task(u"remote_torrent overflow_check",
                           LoopingCall(torrent_overflow_check)).start(TORRENT_OVERFLOW_CHECKING_INTERVAL, now=True)

    def schedule_task(self, name, task, delay_time=0.0, *args, **kwargs):
        self.register_task(name, reactor.callLater(delay_time, task, *args, **kwargs))

    @call_on_reactor_thread
    def download_torrent(self, candidate, infohash, user_callback=None, priority=1, timeout=None):
        assert isinstance(infohash, str), u"infohash has invalid type: %s" % type(infohash)
        assert len(infohash) == INFOHASH_LENGTH, u"infohash has invalid length: %s" % len(infohash)

        # fix prio levels to 1 and 0
        priority = min(priority, 1)

        # check if we have a candidate
        if not candidate:
            # we use DHT
            requester = self.magnet_requesters.get(priority)
            magnet_lambda = lambda ih = infohash: requester.add_request(ih, None)
            self.schedule_task(u"magnet_request", magnet_lambda, delay_time=MAGNET_TIMEOUT * priority)
            return

        requesters = self.torrent_requesters

        # look for lowest prio requester, which already has this infohash scheduled
        requester = None
        for i in range(0, priority + 1):
            if i in requesters and requesters[i].has_requested(infohash):
                requester = requesters[i]
                break

        # if not found, then used/create this requester
        if not requester:
            if priority not in requesters:
                requesters[priority] = TftpTorrentRequester(self.session, self, priority)
            requester = requesters[priority]

        # make request
        if requester:
            if user_callback:
                self.torrent_callbacks.setdefault(infohash, set()).add(user_callback)

            requester.add_request(infohash, candidate, timeout)
            self._logger.info(u"adding torrent request: %s %s %s", hexlify(infohash or ''), candidate, priority)

    def get_torrent_filename(self, infohash):
        return u"%s.torrent" % hexlify(infohash)

    def has_torrent(self, infohash, callback):
        assert isinstance(infohash, str), u"infohash is not str: %s" % type(infohash)
        assert len(infohash) == INFOHASH_LENGTH, u"infohash length is not %s: %s" % (INFOHASH_LENGTH, len(infohash))

        # check torrent collecting directory for the torrent
        torrent_filename = u"%s.torrent" % hexlify(infohash)
        file_path = os.path.join(self.tor_col_dir, torrent_filename)

        has_file = os.path.exists(file_path) and os.path.isfile(file_path)
        callback(has_file)

    def save_torrent(self, tdef, callback=None):
        def do_schedule(filename):
            if not filename:
                self._save_torrent(tdef, callback)
            elif callback:
                @call_on_reactor_thread
                def perform_callback():
                    callback()
                perform_callback()

        infohash = tdef.get_infohash()
        self.has_torrent(infohash, do_schedule)

    def _save_torrent(self, tdef, callback=None):
        # save torrent file to collected_torrent directory
        infohash = tdef.get_infohash()
        des_file_path = os.path.join(self.tor_col_dir, self.get_torrent_filename(infohash))
        tdef.save(des_file_path)

        @call_on_reactor_thread
        def do_db(callback):
            # add this new torrent to db
            if self.torrent_db.hasTorrent(infohash):
                self.torrent_db.updateTorrent(infohash, torrent_file_name=des_file_path)
            else:
                self.torrent_db.addExternalTorrent(tdef, extra_info={'filename': des_file_path, 'status': 'good'})

            # notify all
            self.notify_possible_torrent_infohash(infohash, des_file_path)
            if callback:
                callback()

        if self.torrent_db:
            do_db(callback)
        elif callback:
            callback()

    @call_on_reactor_thread
    def download_torrentmessage(self, candidate, infohash, user_callback=None, priority=1):
        assert isinstance(infohash, str), u"infohash has invalid type: %s" % type(infohash)
        assert len(infohash) == INFOHASH_LENGTH, u"infohash has invalid length: %s" % len(infohash)

        if user_callback:
            callback = lambda ih = infohash: user_callback(ih)
            self.torrent_callbacks.setdefault(infohash, set()).add(callback)

        if priority not in self.torrent_message_requesters:
            self.torrent_message_requesters[priority] = TorrentMessageRequester(self.session, self, priority)

        requester = self.torrent_message_requesters[priority]

        # make request
        requester.add_request(infohash, candidate)
        self._logger.debug(u"adding torrent messages request: %s %s %s", hexlify(infohash), candidate, priority)

    def has_metadata(self, infohash, thumbnail_subpath):
        metadata_filepath = os.path.join(self.tor_col_dir, thumbnail_subpath)
        return os.path.isfile(metadata_filepath)

    def delete_metadata(self, infohash, thumbnail_subpath):
        # delete the folder and the swift files
        metadata_filepath = self.get_metadata_path(infohash, thumbnail_subpath)
        if not os.path.exists(metadata_filepath):
            self._logger.error(u"trying to delete non-existing metadata: %s", metadata_filepath)
        elif not os.path.isfile(metadata_filepath):
            self._logger.error(u"deleting directory while expecting file metadata: %s", metadata_filepath)
            shutil.rmtree(metadata_filepath)
        else:
            os.unlink(metadata_filepath)
            self._logger.debug(u"metadata file deleted: %s", metadata_filepath)

    def get_metadata_path(self, infohash, thumbnail_subpath):
        return os.path.join(self.tor_col_dir, thumbnail_subpath)

    @call_on_reactor_thread
    def download_metadata(self, candidate, infohash, thumbnail_subpath, usercallback=None, timeout=None):
        if self.has_metadata(infohash, thumbnail_subpath):
            return

        if usercallback:
            self.metadata_callbacks.setdefault(infohash, set()).add(usercallback)

        self.metadata_requester.add_request((infohash, thumbnail_subpath), candidate, timeout)

        infohash_str = hexlify(infohash or "")
        self._logger.debug(u"adding metadata request: %s %s %s", infohash_str, thumbnail_subpath, candidate)

    def save_metadata(self, infohash, thumbnail_subpath, data):
        # save data to a temporary tarball and extract it to the torrent collecting directory
        thumbnail_path = self.get_metadata_path(infohash, thumbnail_subpath)
        dir_name = os.path.dirname(thumbnail_path)
        if not os.path.exists(dir_name):
            os.mkdir(dir_name)
        with open(thumbnail_path, "wb") as f:
            f.write(data)

        self.notify_possible_metadata_infohash(infohash)
        if self.metadata_callbacks[infohash]:
            for callback in self.metadata_callbacks[infohash]:
                callback(infohash)

    def notify_possible_torrent_infohash(self, infohash, torrent_file_name=None):
        for key in self.torrent_callbacks:
            if key == infohash:
                handle_lambda = lambda k = key, f = torrent_file_name: self._handleCallback(k, f)
                self.schedule_task(u"notify_torrent", handle_lambda)

    def notify_possible_metadata_infohash(self, infohash):
        metadata_dir = os.path.join(self.tor_col_dir, self.get_metadata_path(u"thumbs", infohash))
        for key in self.metadata_callbacks:
            if key == infohash:
                handle_lambda = lambda k = key, f = metadata_dir: self._handleCallback(k, f)
                self.schedule_task(u"notify_metadata", handle_lambda)

    def _handleCallback(self, key, torrent_file_name=None):
        self._logger.debug(u"got torrent for: %s", (hexlify(key) if isinstance(key, basestring) else key))

        if key in self.torrent_callbacks:
            for usercallback in self.torrent_callbacks[key]:
                self.session.uch.perform_usercallback(lambda ucb=usercallback: ucb(torrent_file_name))

            del self.torrent_callbacks[key]

            if torrent_file_name:
                for requester in self.magnet_requesters.itervalues():
                    if requester.has_requested(key):
                        requester.remove_request(key)
            else:
                for requester in self.torrent_message_requesters.itervalues():
                    if requester.has_requested(key):
                        requester.remove_request(key)

    def getQueueSize(self):
        def getQueueSize(qname, requesters):
            qsize = {}
            for requester in requesters.itervalues():
                if len(requester.sources):
                    qsize[requester.prio] = len(requester.sources)
            items = qsize.items()
            if items:
                items.sort()
                return "%s: " % qname + ",".join(map(lambda a: "%d/%d" % a, items))
            return ''
        return ", ".join([qstring for qstring in [getQueueSize("TQueue", self.torrent_requesters), getQueueSize("DQueue", self.magnet_requesters), getQueueSize("MQueue", self.torrent_message_requesters)] if qstring])

    def getQueueSuccess(self):
        def getQueueSuccess(qname, requesters):
            sum_requests = sum_success = sum_fail = sum_on_disk = 0
            print_value = False
            for requester in requesters.itervalues():
                if requester.requests_success >= 0:
                    print_value = True
                    sum_requests += (requester.requests_made - requester.requests_on_disk)
                    sum_success += requester.requests_success
                    sum_fail += requester.requests_fail
                    sum_on_disk += requester.requests_on_disk

            if print_value:
                return "%s: %d/%d" % (qname, sum_success, sum_requests), "%s: success %d, pending %d, on disk %d, failed %d" % (qname, sum_success, sum_requests - sum_success - sum_fail, sum_on_disk, sum_fail)
            return '', ''
        return [(qstring, qtooltip) for qstring, qtooltip in [getQueueSuccess("TQueue", self.torrent_requesters), getQueueSuccess("DQueue", self.magnet_requesters), getQueueSuccess("MQueue", self.torrent_message_requesters)] if qstring]

    def getBandwidthSpent(self):
        def getQueueBW(qname, requesters):
            bw = 0
            for requester in requesters.itervalues():
                bw += requester.bandwidth
            if bw:
                return "%s: " % qname + "%.1f KB" % (bw / 1024.0)
            return ''
        return ", ".join([qstring for qstring in [getQueueBW("TQueue", self.torrent_requesters), getQueueBW("DQueue", self.magnet_requesters)] if qstring])


class Requester(object):
    __meta__ = ABCMeta

    REQUEST_INTERVAL = 0.5

    def __init__(self, session, remote_torrent_handler, priority):
        self._logger = logging.getLogger(self.__class__.__name__)

        self.session = session
        self.remote_torrent_handler = remote_torrent_handler
        self.priority = priority

        self.queue = Queue.Queue()
        self.download_sources = {}

        self.requests_made = 0
        self.requests_success = 0
        self.requests_failed = 0
        self.requests_on_disk = 0

        self.bandwidth = 0

    def add_request(self, infohash, candidate, timeout=None):
        assert isinstance(infohash, str), u"infohash is not str: %s" % type(infohash)
        assert len(infohash) == INFOHASH_LENGTH, u"infohash length is not %s: %s" % (INFOHASH_LENGTH, len(infohash))

        queue_was_empty = self.queue.empty()

        if infohash not in self.download_sources:
            self.download_sources[infohash] = set()
        self.download_sources[infohash].add(candidate)

        timeout = sys.maxsize if timeout is None else timeout + time()
        self.queue.put((infohash, timeout))

        if queue_was_empty:
            self.remote_torrent_handler.schedule_task(u"requester", self._do_request,
                                                      delay_time=Requester.REQUEST_INTERVAL * self.priority)

    def has_requested(self, infohash):
        return infohash in self.download_sources

    def remove_request(self, infohash):
        del self.download_sources[infohash]

    def _do_request(self):
        try:
            made_request = False

            if self.can_request():
                # request new infohash from queue
                while True:
                    infohash, timeout = self.queue.get_nowait()

                    # check if still needed
                    if time() > timeout:
                        self._logger.debug(u"timeout for infohash %s", hexlify(infohash))

                        if infohash in self.download_sources:
                            self.remove_request(infohash)

                    elif infohash in self.download_sources:
                        break

                    self.queue.task_done()

                try:
                    candidates = list(self.download_sources[infohash])
                    del self.download_sources[infohash]

                    made_request = self.do_fetch(infohash, candidates)
                    if made_request:
                        self.requests_made += 1

                # Make sure exceptions won't crash this requesting loop
                except:
                    print_exc()

                self.queue.task_done()

            if made_request or not self.can_request():
                self.remote_torrent_handler.schedule_task(u"request_%s" % hexlify(infohash), self._do_request,
                                                          delay_time=self.REQUEST_INTERVAL * self.priority)
            else:
                self.remote_torrent_handler.schedule_task(u"request_%s" % hexlify(infohash), self._do_request)
        except Queue.Empty:
            pass

    def can_request(self):
        return True

    @abstractmethod
    def do_fetch(self, infohash, candidates):
        pass


class TorrentMessageRequester(Requester):

    def __init__(self, session, remote_torrent_handler, priority):
        super(TorrentMessageRequester, self).__init__(session, remote_torrent_handler, priority)
        if sys.platform == 'darwin':
            # Arno, 2012-07-25: Mac has just 256 fds per process, be less aggressive
            self.REQUEST_INTERVAL = 1.0

        self.requests_success = -1

    def do_fetch(self, infohash, candidates):
        attempting_download = False
        self._logger.debug(u"requesting torrent message %s %s", hexlify(infohash), candidates)

        for candidate in candidates:
            self._create_search_community_torrent_request(infohash, candidate)
            attempting_download = True

        return attempting_download

    @call_on_reactor_thread
    def _create_search_community_torrent_request(self, infohash, candidate):
        for community in self.session.lm.dispersy.get_communities():
            from Tribler.community.search.community import SearchCommunity
            if isinstance(community, SearchCommunity):
                community.create_torrent_request(infohash, candidate)
                return


class TftpRequester(Requester):

    def __init__(self, session, remote_torrent_handler, priority):
        super(TftpRequester, self).__init__(session, remote_torrent_handler, priority)

        self.untried_sources = {}
        self.tried_sources = {}
        self.pending_request_queue = deque()

    def has_requested(self, key):
        return key in self.pending_request_queue

    @call_on_reactor_thread
    def add_request(self, key, candidate, timeout=None):
        queue_was_empty = len(self.pending_request_queue) == 0

        if key in self.pending_request_queue:
            # append to the active one
            if candidate not in self.untried_sources[key] and candidate not in self.tried_sources[key]:
                self.untried_sources[key].append(candidate)

        else:
            # new request
            if key not in self.untried_sources:
                self.untried_sources[key] = deque()
            self.untried_sources[key].append(candidate)
            if key not in self.tried_sources:
                self.tried_sources[key] = deque()
            self.pending_request_queue.append(key)
        # start if there is no active request
        if queue_was_empty:
            self.remote_torrent_handler.schedule_task(u"tftp_request", self._do_request,
                                                      delay_time=Requester.REQUEST_INTERVAL * self.priority)

    @call_on_reactor_thread
    def _do_request(self):
        if not self.pending_request_queue:
            return

        # starts to download a torrent
        key = self.pending_request_queue[0]

        candidate = self.untried_sources[key].popleft()
        self.tried_sources[key].append(candidate)

        self.do_fetch(key, candidate)

    def _clear_current_request(self):
        key = self.pending_request_queue.popleft()
        del self.untried_sources[key]
        del self.tried_sources[key]

    def do_fetch(self, key, candidate):
        ip, port = candidate.sock_addr
        if isinstance(key, tuple):
            infohash, thumbnail_subpath = key
        else:
            infohash = key
            thumbnail_subpath = None

        self._logger.debug(u"start TFTP download for %s from %s:%s", hexlify(infohash), ip, port)

        if thumbnail_subpath:
            file_name = thumbnail_subpath
        else:
            file_name = self.remote_torrent_handler.get_torrent_filename(infohash)

        extra_info = {u"infohash": infohash, u"thumbnail_subpath": thumbnail_subpath}
        self.session.lm.tftp_handler.download_file(file_name, ip, port, extra_info=extra_info,
                                                   success_callback=self._tftp_success_callback,
                                                   failure_callback=self._tftp_failure_callback)

    @call_on_reactor_thread
    def _tftp_success_callback(self, address, file_name, file_data, extra_info):
        self._logger.debug(u"successfully downloaded %s from %s:%s", file_name, address[0], address[1])
        self.requests_success += 1
        self.bandwidth += len(file_data)

        # start the next request
        self._clear_current_request()
        self.remote_torrent_handler.schedule_task(u"tftp_request", self._do_request,
                                                  delay_time=Requester.REQUEST_INTERVAL * self.priority)

    @call_on_reactor_thread
    def _tftp_failure_callback(self, address, file_name, error_msg, extra_info):
        self._logger.debug(u"failed to download %s from %s:%s: %s", file_name, address[0], address[1], error_msg)
        self.requests_failed += 1

        infohash = extra_info[u"infohash"]
        if self.untried_sources[infohash]:
            # try to download this data from another candidate
            candidate = self.untried_sources[infohash].popleft()
            self.tried_sources[infohash].append(candidate)

            self._logger.debug(u"trying next candidate %s:%s for request %s",
                               candidate.sock_addr[0], candidate.sock_addr[1], hexlify(infohash))
            self.remote_torrent_handler.schedule_task(u"tftp_request",
                                                      lambda ih=infohash, c=candidate: self.do_fetch(ih, c))

        else:
            # no more available candidates, download the next requested infohash
            self._clear_current_request()
            self.remote_torrent_handler.schedule_task(u"tftp_request", self._do_request,
                                                      delay_time=Requester.REQUEST_INTERVAL * self.priority)


class TftpTorrentRequester(TftpRequester):
    """ This is a requester that downloads a torrent file using TFTP.
    """

    def _tftp_success_callback(self, address, file_name, file_data, extra_info):
        super(TftpTorrentRequester, self)._tftp_success_callback(address, file_name, file_data, extra_info)

        # save torrent
        tdef = TorrentDef.load_from_memory(file_data)
        self.remote_torrent_handler.save_torrent(tdef)


class TftpMetadataRequester(TftpRequester):

    def __init__(self, session, remote_torrent_handler):
        super(TftpMetadataRequester, self).__init__(session, remote_torrent_handler, 0)
        if sys.platform == 'darwin':
            # mac has severe problems with closing connections, add additional time to allow it to close connections
            self.REQUEST_INTERVAL = 15.0

    def _tftp_success_callback(self, address, file_name, file_data, extra_info):
        super(TftpMetadataRequester, self)._tftp_success_callback(address, file_name, file_data, extra_info)

        # save metadata
        self.remote_torrent_handler.save_metadata(extra_info[u"infohash"], extra_info[u"thumbnail_subpath"], file_data)


class MagnetRequester(Requester):
    MAX_CONCURRENT = 1
    MAGNET_RETRIEVE_TIMEOUT = 30.0

    def __init__(self, session, remote_torrent_handler, priority):
        super(MagnetRequester, self).__init__(session, remote_torrent_handler, priority)
        if sys.platform == 'darwin':
            # mac has severe problems with closing connections, add additional time to allow it to close connections
            self.REQUEST_INTERVAL = 15.0

        self.requested_infohashes = set()

        if priority <= 1 and not sys.platform == 'darwin':
            self.MAX_CONCURRENT = 3

    def can_request(self):
        return len(self.requested_infohashes) < self.MAX_CONCURRENT

    @call_on_reactor_thread
    def do_fetch(self, infohash, candidates):
        if infohash not in self.requested_infohashes:
            self.requested_infohashes.add(infohash)

            raw_lambda = lambda filename, ih = infohash: self._do_fetch(filename, ih)
            self.remote_torrent_handler.has_torrent(infohash, raw_lambda)
            return True

    @call_on_reactor_thread
    def _do_fetch(self, filename, infohash):
        if filename:
            if infohash in self.requested_infohashes:
                self.requested_infohashes.remove(infohash)

            self.remote_torrent_handler.notify_possible_torrent_infohash(infohash, filename)
            self.requests_on_disk += 1

        else:
            infohash_str = hexlify(infohash)
            # try magnet link
            magnetlink = "magnet:?xt=urn:btih:" + infohash_str

            if self.remote_torrent_handler.torrent_db:
                # see if we know any trackers for this magnet
                trackers = self.remote_torrent_handler.torrent_db.getTrackerListByInfohash(infohash)
                for tracker in trackers:
                    if tracker not in (u"no-DHT", u"DHT"):
                        magnetlink += "&tr=" + urllib.quote_plus(tracker)

            self._logger.debug(u"requesting magnet %s %s %s", infohash_str, self.priority, magnetlink)

            TorrentDef.retrieve_from_magnet(magnetlink, self._torrentdef_retrieved, timeout=self.MAGNET_RETRIEVE_TIMEOUT,
                                            timeout_callback=self._torrentdef_failed, silent=True)
            return True

    @call_on_reactor_thread
    def _torrentdef_retrieved(self, tdef):
        infohash = tdef.get_infohash()
        self._logger.debug(u"received torrent using magnet %s", hexlify(infohash))

        self.remote_torrent_handler.save_torrent(tdef)
        if infohash in self.requested_infohashes:
            self.requested_infohashes.remove(infohash)

        self.requests_success += 1
        self.bandwidth += tdef.get_torrent_size()

    @call_on_reactor_thread
    def _torrentdef_failed(self, infohash):
        if infohash in self.requested_infohashes:
            self.requested_infohashes.remove(infohash)

        self.requests_failed += 1
