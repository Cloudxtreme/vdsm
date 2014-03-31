#
# Copyright 2008-2014 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

import threading
import time

import libvirt

import hooks
import kaxmlrpclib
from vdsm import utils
from vdsm import vdscli
from vdsm.compat import pickle
from vdsm.config import config
from vdsm.define import NORMAL, errCode, Mbytes

from . import vmexitreason


class SourceThread(threading.Thread):
    """
    A thread that takes care of migration on the source vdsm.
    """
    _ongoingMigrations = threading.BoundedSemaphore(1)

    @classmethod
    def setMaxOutgoingMigrations(cls, n):
        """Set the initial value of the _ongoingMigrations semaphore.

        must not be called after any vm has been run."""
        cls._ongoingMigrations = threading.BoundedSemaphore(n)

    def __init__(self, vm, dst='', dstparams='',
                 mode='remote', method='online',
                 tunneled=False, dstqemu='', abortOnError=False, **kwargs):
        self.log = vm.log
        self._vm = vm
        self._dst = dst
        self._mode = mode
        self._method = method
        self._dstparams = dstparams
        self._machineParams = {}
        self._tunneled = utils.tobool(tunneled)
        self._abortOnError = utils.tobool(abortOnError)
        self._dstqemu = dstqemu
        self._downtime = kwargs.get('downtime') or \
            config.get('vars', 'migration_downtime')
        self.status = {
            'status': {
                'code': 0,
                'message': 'Migration in progress'},
            'progress': 0}
        threading.Thread.__init__(self)
        self._preparingMigrationEvt = True
        self._migrationCanceledEvt = False
        self._monitorThread = None

    def getStat(self):
        """
        Get the status of the migration.
        """
        if self._monitorThread is not None:
            # fetch migration status from the monitor thread
            self.status['progress'] = self._monitorThread.progress
        return self.status

    def _setupVdsConnection(self):
        if self._mode == 'file':
            return

        # FIXME: The port will depend on the binding being used.
        # This assumes xmlrpc
        hostPort = vdscli.cannonizeHostPort(
            self._dst, self._vm.cif.bindings['xmlrpc'].serverPort)
        self.remoteHost, self.remotePort = hostPort.rsplit(':', 1)

        if config.getboolean('vars', 'ssl'):
            self.destServer = vdscli.connect(
                hostPort,
                useSSL=True,
                TransportClass=kaxmlrpclib.TcpkeepSafeTransport)
        else:
            self.destServer = kaxmlrpclib.Server('http://' + hostPort)
        self.log.debug('Destination server is: ' + hostPort)
        try:
            self.log.debug('Initiating connection with destination')
            status = self.destServer.getVmStats(self._vm.id)
            if not status['status']['code']:
                self.log.error("Machine already exists on the destination")
                self.status = errCode['exist']
        except Exception:
            self.log.error("Error initiating connection", exc_info=True)
            self.status = errCode['noConPeer']

    def _setupRemoteMachineParams(self):
        self._machineParams.update(self._vm.status())
        # patch VM config for targets < 3.1
        self._patchConfigForLegacy()
        self._machineParams['elapsedTimeOffset'] = \
            time.time() - self._vm._startTime
        vmStats = self._vm.getStats()
        if 'username' in vmStats:
            self._machineParams['username'] = vmStats['username']
        if 'guestIPs' in vmStats:
            self._machineParams['guestIPs'] = vmStats['guestIPs']
        if 'guestFQDN' in vmStats:
            self._machineParams['guestFQDN'] = vmStats['guestFQDN']
        for k in ('_migrationParams', 'pid'):
            if k in self._machineParams:
                del self._machineParams[k]
        if self._mode != 'file':
            self._machineParams['migrationDest'] = 'libvirt'
        self._machineParams['_srcDomXML'] = self._vm._dom.XMLDesc(0)

    def _prepareGuest(self):
        if self._mode == 'file':
            self.log.debug("Save State begins")
            if self._vm.guestAgent.isResponsive():
                lockTimeout = 30
            else:
                lockTimeout = 0
            self._vm.guestAgent.desktopLock()
            # wait for lock or timeout
            while lockTimeout:
                if self._vm.getStats()['session'] in ["Locked", "LoggedOff"]:
                    break
                time.sleep(1)
                lockTimeout -= 1
                if lockTimeout == 0:
                    self.log.warning('Agent ' + self._vm.id +
                                     ' unresponsive. Hiberanting without '
                                     'desktopLock.')
                    break
            self._vm.pause('Saving State')
        else:
            self.log.debug("Migration started")
            self._vm.lastStatus = 'Migration Source'

    def _recover(self, message):
        if not self.status['status']['code']:
            self.status = errCode['migrateErr']
        self.log.error(message)
        if self._mode != 'file':
            try:
                self.destServer.destroy(self._vm.id)
            except Exception:
                self.log.error("Failed to destroy remote VM", exc_info=True)
        # if the guest was stopped before migration, we need to cont it
        if self._mode == 'file' or self._method != 'online':
            self._vm.cont()
        # either way, migration has finished
        self._vm.lastStatus = 'Up'

    def _finishSuccessfully(self):
        self.status['progress'] = 100
        if self._mode != 'file':
            self._vm.setDownStatus(NORMAL, vmexitreason.MIGRATION_SUCCEEDED)
            self.status['status']['message'] = 'Migration done'
        else:
            # don't pickle transient params
            for ignoreParam in ('displayIp', 'display', 'pid'):
                if ignoreParam in self._machineParams:
                    del self._machineParams[ignoreParam]

            fname = self._vm.cif.prepareVolumePath(self._dstparams)
            try:
                with open(fname, "w") as f:
                    pickle.dump(self._machineParams, f)
            finally:
                self._vm.cif.teardownVolumePath(self._dstparams)

            self._vm.setDownStatus(NORMAL, vmexitreason.SAVE_STATE_SUCCEEDED)
            self.status['status']['message'] = 'SaveState done'

    def _patchConfigForLegacy(self):
        """
        Remove from the VM config drives list "cdrom" and "floppy"
        items and set them up as full paths
        """
        # care only about "drives" list, since
        # "devices" doesn't cause errors
        if 'drives' in self._machineParams:
            for item in ("cdrom", "floppy"):
                new_drives = []
                for drive in self._machineParams['drives']:
                    if drive['device'] == item:
                        self._machineParams[item] = drive['path']
                    else:
                        new_drives.append(drive)
                self._machineParams['drives'] = new_drives

        # vdsm < 4.13 expect this to exist
        self._machineParams['afterMigrationStatus'] = ''

    @staticmethod
    def _raiseAbortError():
        e = libvirt.libvirtError(defmsg='')
        # we have to override the value to get what we want
        # err might be None
        e.err = (libvirt.VIR_ERR_OPERATION_ABORTED,  # error code
                 libvirt.VIR_FROM_QEMU,              # error domain
                 'operation aborted',                # error message
                 libvirt.VIR_ERR_WARNING,            # error level
                 '', '', '',                         # str1, str2, str3,
                 -1, -1)                             # int1, int2
        raise e

    def run(self):
        try:
            startTime = time.time()
            self._setupVdsConnection()
            self._setupRemoteMachineParams()
            self._prepareGuest()
            SourceThread._ongoingMigrations.acquire()
            try:
                if self._migrationCanceledEvt:
                    self._raiseAbortError()
                self.log.debug("migration semaphore acquired")
                self._vm.conf['_migrationParams'] = {
                    'dst': self._dst,
                    'mode': self._mode,
                    'method': self._method,
                    'dstparams': self._dstparams,
                    'dstqemu': self._dstqemu}
                self._vm.saveState()
                self._startUnderlyingMigration(startTime)
                self._finishSuccessfully()
            except libvirt.libvirtError as e:
                if e.get_error_code() == libvirt.VIR_ERR_OPERATION_ABORTED:
                    self.status['status']['code'] = \
                        errCode['migCancelErr']['status']['code']
                    self.status['status']['message'] = 'Migration canceled'
                raise
            finally:
                if '_migrationParams' in self._vm.conf:
                    del self._vm.conf['_migrationParams']
                SourceThread._ongoingMigrations.release()
        except Exception as e:
            self._recover(str(e))
            self.log.error("Failed to migrate", exc_info=True)

    def _startUnderlyingMigration(self, startTime):
        if self._mode == 'file':
            hooks.before_vm_hibernate(self._vm._dom.XMLDesc(0), self._vm.conf)
            try:
                self._vm._vmStats.pause()
                fname = self._vm.cif.prepareVolumePath(self._dst)
                try:
                    self._vm._dom.save(fname)
                finally:
                    self._vm.cif.teardownVolumePath(self._dst)
            except Exception:
                self._vm._vmStats.cont()
                raise
        else:
            for dev in self._vm._customDevices():
                hooks.before_device_migrate_source(
                    dev._deviceXML, self._vm.conf, dev.custom)
            hooks.before_vm_migrate_source(self._vm._dom.XMLDesc(0),
                                           self._vm.conf)
            response = self.destServer.migrationCreate(self._machineParams)
            if response['status']['code']:
                self.status = response
                raise RuntimeError('migration destination error: ' +
                                   response['status']['message'])
            if config.getboolean('vars', 'ssl'):
                transport = 'tls'
            else:
                transport = 'tcp'
            duri = 'qemu+%s://%s/system' % (transport, self.remoteHost)
            if self._vm.conf['_migrationParams']['dstqemu']:
                muri = 'tcp://%s' % \
                       self._vm.conf['_migrationParams']['dstqemu']
            else:
                muri = 'tcp://%s' % self.remoteHost

            self._vm.log.debug('starting migration to %s '
                               'with miguri %s', duri, muri)

            t = DowntimeThread(self._vm, int(self._downtime))

            if MonitorThread._MIGRATION_MONITOR_INTERVAL:
                self._monitorThread = MonitorThread(self._vm, startTime)
                self._monitorThread.start()

            try:
                if ('qxl' in self._vm.conf['display'] and
                        self._vm.conf.get('clientIp')):
                    SPICE_MIGRATION_HANDOVER_TIME = 120
                    self._vm._reviveTicket(SPICE_MIGRATION_HANDOVER_TIME)

                maxBandwidth = config.getint('vars', 'migration_max_bandwidth')
                # FIXME: there still a race here with libvirt,
                # if we call stop() and libvirt migrateToURI2 didn't start
                # we may return migration stop but it will start at libvirt
                # side
                self._preparingMigrationEvt = False
                if not self._migrationCanceledEvt:
                    self._vm._dom.migrateToURI2(
                        duri, muri, None,
                        libvirt.VIR_MIGRATE_LIVE |
                        libvirt.VIR_MIGRATE_PEER2PEER |
                        (libvirt.VIR_MIGRATE_TUNNELLED if
                            self._tunneled else 0) |
                        (libvirt.VIR_MIGRATE_ABORT_ON_ERROR if
                            self._abortOnError else 0),
                        None, maxBandwidth)
                else:
                    self._raiseAbortError()

            finally:
                t.cancel()
                if MonitorThread._MIGRATION_MONITOR_INTERVAL:
                    self._monitorThread.stop()

    def stop(self):
        # if its locks we are before the migrateToURI2()
        # call so no need to abortJob()
        try:
            self._migrationCanceledEvt = True
            self._vm._dom.abortJob()
        except libvirt.libvirtError:
            if not self._preparingMigrationEvt:
                    raise


class DowntimeThread(threading.Thread):
    def __init__(self, vm, downtime):
        super(DowntimeThread, self).__init__()
        self.DOWNTIME_STEPS = config.getint('vars', 'migration_downtime_steps')

        self._vm = vm
        self._downtime = downtime
        self._stop = threading.Event()

        delay_per_gib = config.getint('vars', 'migration_downtime_delay')
        memSize = int(vm.conf['memSize'])
        self._wait = (delay_per_gib * max(memSize, 2048) + 1023) / 1024

        self.daemon = True
        self.start()

    def run(self):
        self._vm.log.debug('migration downtime thread started')

        for i in range(self.DOWNTIME_STEPS):
            self._stop.wait(self._wait / self.DOWNTIME_STEPS)

            if self._stop.isSet():
                break

            downtime = self._downtime * (i + 1) / self.DOWNTIME_STEPS
            self._vm.log.debug('setting migration downtime to %d', downtime)
            self._vm._dom.migrateSetMaxDowntime(downtime, 0)

        self._vm.log.debug('migration downtime thread exiting')

    def cancel(self):
        self._vm.log.debug('canceling migration downtime thread')
        self._stop.set()


class MonitorThread(threading.Thread):
    _MIGRATION_MONITOR_INTERVAL = config.getint(
        'vars', 'migration_monitor_interval')  # seconds

    def __init__(self, vm, startTime):
        super(MonitorThread, self).__init__()
        self._stop = threading.Event()
        self._vm = vm
        self._startTime = startTime
        self.daemon = True
        self.progress = 0

    def run(self):
        def calculateProgress(remaining, total):
            if remaining == 0:
                return 100
            progress = 100 - 100 * remaining / total if total else 0
            return progress if (progress < 100) else 99

        self._vm.log.debug('starting migration monitor thread')

        memSize = int(self._vm.conf['memSize'])
        maxTimePerGiB = config.getint('vars',
                                      'migration_max_time_per_gib_mem')
        migrationMaxTime = (maxTimePerGiB * memSize + 1023) / 1024
        lastProgressTime = time.time()
        lowmark = None
        progress_timeout = config.getint('vars', 'migration_progress_timeout')

        while not self._stop.isSet():
            self._stop.wait(self._MIGRATION_MONITOR_INTERVAL)
            (jobType, timeElapsed, _,
             dataTotal, dataProcessed, dataRemaining,
             memTotal, memProcessed, memRemaining,
             fileTotal, fileProcessed, _) = self._vm._dom.jobInfo()
            # from libvirt sources: data* = file* + mem*.
            # docs can be misleading due to misaligned lines.
            abort = False
            now = time.time()
            if 0 < migrationMaxTime < now - self._startTime:
                self._vm.log.warn('The migration took %d seconds which is '
                                  'exceeding the configured maximum time '
                                  'for migrations of %d seconds. The '
                                  'migration will be aborted.',
                                  now - self._startTime,
                                  migrationMaxTime)
                abort = True
            elif (lowmark is None) or (lowmark > dataRemaining):
                lowmark = dataRemaining
                lastProgressTime = now
            elif (now - lastProgressTime) > progress_timeout:
                # Migration is stuck, abort
                self._vm.log.warn(
                    'Migration is stuck: Hasn\'t progressed in %s seconds. '
                    'Aborting.' % (now - lastProgressTime))
                abort = True

            if abort:
                self._vm._dom.abortJob()
                self.stop()
                break

            if dataRemaining > lowmark:
                self._vm.log.warn(
                    'Migration stalling: remaining (%sMiB)'
                    ' > lowmark (%sMiB).'
                    ' Refer to RHBZ#919201.',
                    dataRemaining / Mbytes, lowmark / Mbytes)

            if jobType == 0:
                continue

            self.progress = calculateProgress(dataRemaining, dataTotal)

            self._vm.log.info('Migration Progress: %s seconds elapsed, %s%% of'
                              ' data processed' %
                              (timeElapsed / 1000, self.progress))

    def stop(self):
        self._vm.log.debug('stopping migration monitor thread')
        self._stop.set()
