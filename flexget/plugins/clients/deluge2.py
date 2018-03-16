from __future__ import unicode_literals, division, absolute_import

import base64
import re
from builtins import *  # noqa pylint: disable=unused-import, redefined-builtin

import logging
import os

import time

from flexget import plugin
from flexget.entry import Entry
from flexget.event import event
from utils.pathscrub import pathscrub
from utils.template import RenderError

log = logging.getLogger('deluge3')


class DelugePlugin(object):
    """Base class for deluge plugins, contains settings and methods for connecting to a deluge daemon."""

    def on_task_start(self, task, config):
        """Raise a DependencyError if our dependencies aren't available"""
        try:
            from deluge_client import DelugeRPCClient
        except ImportError as e:
            log.debug('Error importing deluge-client: %s' % e)
            raise plugin.DependencyError('deluge', 'deluge-client',
                                         'deluge-client >=1.2 module and it\'s dependencies required. ImportError: %s' %
                                         e, log)
        config = self.prepare_config(config)
        self.client = DelugeRPCClient(config['host'], config['port'], config['username'], config['password'],
                                      decode_utf8=True, deluge_version=2)

    def on_task_abort(self, task, config):
        pass

    def prepare_config(self, config):
        config.setdefault('host', 'localhost')
        config.setdefault('port', 58846)
        return config

    def connect(self):
        """Connects to the deluge daemon and runs on_connect_success """

        self.client.connect()

        if not self.client.connected:
            raise plugin.PluginError('Deluge failed to connect.')

    def disconnect(self):
        self.client.disconnect()

    def get_torrents_status(self, fields, filters=None):
        """Fetches all torrents and their requested fields optionally filtered"""
        if filters is None:
            filters = {}
        return self.client.call('core.get_torrents_status', filters, fields)


class InputDeluge(DelugePlugin):
    """Create entries for torrents in the deluge session."""
    #
    settings_map = {
        'name': 'title',
        'hash': 'torrent_info_hash',
        'num_peers': 'torrent_peers',
        'num_seeds': 'torrent_seeds',
        'progress': 'deluge_progress',
        'seeding_time': ('deluge_seed_time', lambda time: time / 3600),
        'private': 'deluge_private',
        'state': 'deluge_state',
        'eta': 'deluge_eta',
        'ratio': 'deluge_ratio',
        'move_on_completed_path': 'deluge_movedone',
        'save_path': 'deluge_path',
        'label': 'deluge_label',
        'total_size': ('content_size', lambda size: size / 1024 / 1024),
        'files': ('content_files', lambda file_dicts: [f['path'] for f in file_dicts])}

    extra_settings_map = {
        'active_time': ('active_time', lambda time: time / 3600),
        'compact': 'compact',
        'distributed_copies': 'distributed_copies',
        'download_payload_rate': 'download_payload_rate',
        'file_progress': 'file_progress',
        'is_auto_managed': 'is_auto_managed',
        'is_seed': 'is_seed',
        'max_connections': 'max_connections',
        'max_download_speed': 'max_download_speed',
        'max_upload_slots': 'max_upload_slots',
        'max_upload_speed':  'max_upload_speed',
        'message': 'message',
        'move_on_completed': 'move_on_completed',
        'next_announce': 'next_announce',
        'num_files': 'num_files',
        'num_pieces': 'num_pieces',
        'paused': 'paused',
        'peers': 'peers',
        'piece_length': 'piece_length',
        'prioritize_first_last': 'prioritize_first_last',
        'queue': 'queue',
        'remove_at_ratio': 'remove_at_ratio',
        'seed_rank': 'seed_rank',
        'stop_at_ratio': 'stop_at_ratio',
        'stop_ratio': 'stop_ratio',
        'total_done': 'total_done',
        'total_payload_download': 'total_payload_download',
        'total_payload_upload': 'total_payload_upload',
        'total_peers': 'total_peers',
        'total_seeds': 'total_seeds',
        'total_uploaded': 'total_uploaded',
        'total_wanted': 'total_wanted',
        'tracker': 'tracker',
        'tracker_host': 'tracker_host',
        'tracker_status': 'tracker_status',
        'trackers': 'trackers',
        'upload_payload_rate': 'upload_payload_rate'
    }

    def __init__(self):
        self.entries = []

    schema = {
        'anyOf': [
            {'type': 'boolean'},
            {
                'type': 'object',
                'properties': {
                    'host': {'type': 'string'},
                    'port': {'type': 'integer'},
                    'username': {'type': 'string'},
                    'password': {'type': 'string'},
                    'config_path': {'type': 'string', 'format': 'path'},
                    'filter': {
                        'type': 'object',
                        'properties': {
                            'label': {'type': 'string'},
                            'state': {
                                'type': 'string',
                                'enum': ['active', 'downloading', 'seeding', 'queued', 'paused']
                            }
                        },
                        'additionalProperties': False
                    },
                    'keys': {
                        'type': 'array',
                        'items': {
                            'type': 'string',
                            'enum': list(extra_settings_map)
                        }
                    }
                },
                'additionalProperties': False
            }
        ]
    }

    def on_task_start(self, task, config):
        config = self.prepare_config(config)
        super(InputDeluge, self).on_task_start(task, config)

    def prepare_config(self, config):
        if isinstance(config, bool):
            config = {}
        if 'filter' in config:
            filter = config['filter']
            if 'label' in filter:
                filter['label'] = filter['label'].lower()
            if 'state' in filter:
                filter['state'] = filter['state'].capitalize()
        super(InputDeluge, self).prepare_config(config)
        return config

    def on_task_input(self, task, config):
        """Generates and returns a list of entries from the deluge daemon."""
        # Reset the entries list
        self.entries = []
        # Call connect, entries get generated if everything is successful
        self.connect()

        self.entries = self.generate_entries(config)
        self.disconnect()
        return self.entries

    def generate_entries(self, config):
        entries = []
        torrents = self.get_torrents_status(list(self.settings_map.keys()) + config.get('keys', []))
        for hash, torrent_dict in torrents.items():
            # Make sure it has a url so no plugins crash
            entry = Entry(deluge_id=hash, url='')
            config_path = os.path.expanduser(config.get('config_path', ''))
            if config_path:
                torrent_path = os.path.join(config_path, 'state', hash + '.torrent')
                if os.path.isfile(torrent_path):
                    entry['location'] = torrent_path
                    if not torrent_path.startswith('/'):
                        torrent_path = '/' + torrent_path
                    entry['url'] = 'file://' + torrent_path
                else:
                    log.warning('Did not find torrent file at %s' % torrent_path)
            for key, value in torrent_dict.items():
                if key in self.settings_map:
                    flexget_key = self.settings_map[key]
                else:
                    flexget_key = self.extra_settings_map[key]
                if isinstance(flexget_key, tuple):
                    flexget_key, format_func = flexget_key
                    value = format_func(value)
                entry[flexget_key] = value
            entries.append(entry)

        return entries


class OutputDeluge(DelugePlugin):
    """Add the torrents directly to deluge, supporting custom save paths."""
    schema = {
        'anyOf': [
            {'type': 'boolean'},
            {
                'type': 'object',
                'properties': {
                    'host': {'type': 'string'},
                    'port': {'type': 'integer'},
                    'username': {'type': 'string'},
                    'password': {'type': 'string'},
                    'path': {'type': 'string'},
                    'movedone': {'type': 'string'},
                    'label': {'type': 'string'},
                    'queuetotop': {'type': 'boolean'},
                    'automanaged': {'type': 'boolean'},
                    'maxupspeed': {'type': 'number'},
                    'maxdownspeed': {'type': 'number'},
                    'maxconnections': {'type': 'integer'},
                    'maxupslots': {'type': 'integer'},
                    'ratio': {'type': 'number'},
                    'removeatratio': {'type': 'boolean'},
                    'addpaused': {'type': 'boolean'},
                    'compact': {'type': 'boolean'},
                    'content_filename': {'type': 'string'},
                    'main_file_only': {'type': 'boolean'},
                    'main_file_ratio': {'type': 'number'},
                    'magnetization_timeout': {'type': 'integer'},
                    'keep_subs': {'type': 'boolean'},
                    'hide_sparse_files': {'type': 'boolean'},
                    'enabled': {'type': 'boolean'},
                    'container_directory': {'type': 'string'},
                },
                'additionalProperties': False
            }
        ]
    }

    def prepare_config(self, config):
        if isinstance(config, bool):
            config = {'enabled': config}
        super(OutputDeluge, self).prepare_config(config)
        config.setdefault('enabled', True)
        config.setdefault('path', '')
        config.setdefault('movedone', '')
        config.setdefault('label', '')
        config.setdefault('main_file_ratio', 0.90)
        config.setdefault('magnetization_timeout', 0)
        config.setdefault('keep_subs', True)  # does nothing without 'content_filename' or 'main_file_only' enabled
        config.setdefault('hide_sparse_files', False)  # does nothing without 'main_file_only' enabled
        return config

    def __init__(self):
        self.deluge_version = None
        self.options = {'maxupspeed': 'max_upload_speed', 'maxdownspeed': 'max_download_speed',
                        'maxconnections': 'max_connections', 'maxupslots': 'max_upload_slots',
                        'automanaged': 'auto_managed', 'ratio': 'stop_ratio', 'removeatratio': 'remove_at_ratio',
                        'addpaused': 'add_paused', 'compact': 'compact_allocation'}

    @plugin.priority(120)
    def on_task_download(self, task, config):
        """
        Call download plugin to generate the temp files we will load into deluge
        then verify they are valid torrents
        """
        config = self.prepare_config(config)
        if not config['enabled']:
            return
        # If the download plugin is not enabled, we need to call it to get our temp .torrent files
        if 'download' not in task.config:
            download = plugin.get_plugin_by_name('download')
            for entry in task.accepted:
                if not entry.get('deluge_id'):
                    download.instance.get_temp_file(task, entry, handle_magnets=True)

    @plugin.priority(135)
    def on_task_output(self, task, config):
        """Add torrents to deluge at exit."""
        config = self.prepare_config(config)
        # don't add when learning
        if task.options.learn:
            return
        if not config['enabled'] or not (task.accepted or task.options.test):
            return

        self.connect()

        if task.options.test:
            log.debug('Test connection to deluge daemon successful.')
            self.client.disconnect()
            return

        # loop through entries to get a list of labels to add
        labels = set()
        for entry in task.accepted:
            label = entry.get('label', config.get('label'))
            if label:
                try:
                    label = self._format_label(entry.render(entry.get('label', config.get('label'))))
                    log.debug('Rendered label: %s', label)
                except RenderError as e:
                    log.error('Error rendering label `%s`: %s', label, e)
                    continue
                labels.add(label)
        if labels:
            # Make sure the label plugin is available and enabled, then add appropriate labels

            enabled_plugins = self.client.call('core.get_enabled_plugins')
            label_enabled = 'Label' in enabled_plugins
            if not label_enabled:
                available_plugins = self.client.call('core.get_available_plugins')
                if 'Label' in available_plugins:
                    log.debug('Enabling label plugin in deluge')
                    label_enabled = self.client.call('core.enable_plugin', 'Label')
                else:
                    log.error('Label plugin is not installed in deluge')

            if label_enabled:
                d_labels = self.client.call('label.get_labels')
                for label in labels:
                    if label not in d_labels:
                        log.debug('Adding the label `%s` to deluge', label)
                        self.client.call('label.add', label)

        # add the torrents
        torrent_ids = self.client.call('core.get_session_state')
        for entry in task.accepted:
            # Generate deluge options dict for torrent add
            add_opts = {}
            try:
                path = entry.render(entry.get('path', config['path']))
                if path:
                    add_opts['download_location'] = pathscrub(os.path.expanduser(path))
            except RenderError as e:
                log.error('Could not set path for %s: %s' % (entry['title'], e))
            for fopt, dopt in self.options.items():
                value = entry.get(fopt, config.get(fopt))
                if value is not None:
                    add_opts[dopt] = value
                    if fopt == 'ratio':
                        add_opts['stop_at_ratio'] = True
            # Make another set of options, that get set after the torrent has been added
            modify_opts = {
                'queuetotop': entry.get('queuetotop', config.get('queuetotop')),
                'main_file_only': entry.get('main_file_only', config.get('main_file_only', False)),
                'main_file_ratio': entry.get('main_file_ratio', config.get('main_file_ratio')),
                'hide_sparse_files': entry.get('hide_sparse_files', config.get('hide_sparse_files', True)),
                'keep_subs': entry.get('keep_subs', config.get('keep_subs', True)),
                'container_directory': config.get('container_directory', '')
            }
            try:
                label = entry.render(entry.get('label', config['label']))
                modify_opts['label'] = self._format_label(label)
            except RenderError as e:
                log.error('Error setting label for `%s`: %s', entry['title'], e)
            try:
                movedone = entry.render(entry.get('movedone', config['movedone']))
                modify_opts['movedone'] = pathscrub(os.path.expanduser(movedone))
            except RenderError as e:
                log.error('Error setting movedone for %s: %s' % (entry['title'], e))
            try:
                content_filename = entry.get('content_filename', config.get('content_filename', ''))
                modify_opts['content_filename'] = pathscrub(entry.render(content_filename))
            except RenderError as e:
                log.error('Error setting content_filename for %s: %s' % (entry['title'], e))

            torrent_id = entry.get('deluge_id') or entry.get('torrent_info_hash')
            torrent_id = torrent_id and torrent_id.lower()
            if torrent_id in torrent_ids:
                log.info('%s is already loaded in deluge, setting options' % entry['title'])
                # Entry has a deluge id, verify the torrent is still in the deluge session and apply options
                # Since this is already loaded in deluge, we may also need to change the path
                modify_opts['path'] = add_opts.pop('download_location', None)
                self.client.call('core.set_torrent_options', [torrent_id], add_opts)
                self._set_torrent_options(torrent_id, entry, modify_opts)
            else:
                magnet, filedump = None, None
                if entry.get('url', '').startswith('magnet:'):
                    magnet = entry['url']
                else:
                    if not os.path.exists(entry['file']):
                        entry.fail('Downloaded temp file \'%s\' doesn\'t exist!' % entry['file'])
                        del (entry['file'])
                        return
                    with open(entry['file'], 'rb') as f:
                        filedump = base64.encodestring(f.read())

                log.verbose('Adding %s to deluge.' % entry['title'])
                added_torrent = None
                if magnet:
                    added_torrent = self.client.call('core.add_torrent_magnet', magnet, add_opts)
                    if config.get('magnetization_timeout'):
                        timeout = config['magnetization_timeout']
                        log.verbose('Waiting %d seconds for "%s" to magnetize' % (timeout, entry['title']))
                        for _ in range(timeout):
                            time.sleep(1)
                            try:
                                status = self.client.call('core.get_torrent_status', torrent_id, ['files'])
                            except Exception as err:
                                log.error('wait_for_metadata Error: %s' % err)
                                break
                            if status.get('files'):
                                log.info('"%s" magnetization successful' % (entry['title']))
                                break
                        else:
                            log.warning('"%s" did not magnetize before the timeout elapsed, '
                                        'file list unavailable for processing.' % entry['title'])
                else:
                    try:
                        added_torrent = self.client.call('core.add_torrent_file', entry['title'], filedump, add_opts)
                    except Exception as e:
                        log.info('%s was not added to deluge! %s' % (entry['title'], e))
                        entry.fail('Could not be added to deluge')
                self._set_torrent_options(added_torrent, entry, modify_opts)

    def on_task_learn(self, task, config):
        """ Make sure all temp files are cleaned up when entries are learned """
        # If download plugin is enabled, it will handle cleanup.
        if 'download' not in task.config:
            download = plugin.get_plugin_by_name('download')
            download.instance.cleanup_temp_files(task)

    def on_task_abort(self, task, config):
        """Make sure normal cleanup tasks still happen on abort."""
        DelugePlugin.on_task_abort(self, task, config)
        self.on_task_learn(task, config)

    def _format_label(self, label):
        """Makes a string compliant with deluge label naming rules"""
        return re.sub('[^\w-]+', '_', label.lower())

    def _set_torrent_options(self, torrent_id, entry, opts):
        """Gets called when a torrent was added to the daemon."""
        log.info('%s successfully added to deluge.' % entry['title'])
        entry['deluge_id'] = torrent_id

        if opts.get('movedone'):
            self.client.call('core.set_torrent_move_completed', torrent_id, True)
            self.client.call('core.set_torrent_move_completed_path', torrent_id, opts['movedone'])
            log.debug('%s move on complete set to %s' % (entry['title'], opts['movedone']))
        if opts.get('label'):
            self.client.call('label.set_torrent', torrent_id, opts['label'])
        if opts.get('queuetotop') is not None:
            if opts['queuetotop']:
                self.client.call('core.queue_top', [torrent_id])
                log.debug('%s moved to top of queue' % entry['title'])
            else:
                self.client.call('core.queue_bottom', [torrent_id])
                log.debug('%s moved to bottom of queue' % entry['title'])


        status_keys = ['files', 'total_size', 'save_path', 'move_on_completed_path',
                       'move_on_completed', 'progress']
        status = self.client.call('core.get_torrent_status', torrent_id, status_keys)
        # Determine where the file should be
        move_now_path = None
        if opts.get('movedone'):
            if status['progress'] == 100:
                move_now_path = opts['movedone']
            else:
                # Deluge will unset the move completed option if we move the storage, forgo setting proper
                # path, in favor of leaving proper final location.
                log.debug('Not moving storage for %s, as this will prevent movedone.' % entry['title'])
        elif opts.get('path'):
            move_now_path = opts['path']

        if move_now_path and os.path.normpath(move_now_path) != os.path.normpath(status['save_path']):
            log.debug('Moving storage for %s to %s' % (entry['title'], move_now_path))
            self.client.call('core.move_storage', [torrent_id], move_now_path)

        big_file_name = ''
        if opts.get('content_filename') or opts.get('main_file_only'):
            # find a file that makes up more than main_file_ratio (default: 90%) of the total size
            main_file = None
            for file in status['files']:
                if file['size'] > (status['total_size'] * opts.get('main_file_ratio')):
                    main_file = file
                    break

            def file_exists(filename):
                # Checks the download path as well as the move completed path for existence of the file
                if os.path.exists(os.path.join(status['save_path'], filename)):
                    return True
                elif status.get('move_on_completed') and status.get('move_on_completed_path'):
                    if os.path.exists(os.path.join(status['move_on_completed_path'], filename)):
                        return True
                else:
                    return False

            def unused_name(name):
                # If on local computer, tries appending a (#) suffix until a unique filename is found
                if self.client.call('is_localhost'):
                    counter = 2
                    while file_exists(name):
                        name = ''.join([os.path.splitext(name)[0],
                                        " (", str(counter), ')',
                                        os.path.splitext(name)[1]])
                        counter += 1
                else:
                    log.debug('Cannot ensure content_filename is unique '
                              'when adding to a remote deluge daemon.')
                return name

            def rename(file, new_name):
                # Renames a file in torrent
                self.client.call('core.rename_files', torrent_id, [(file['index'], new_name)])
                log.debug('File %s in %s renamed to %s' % (file['path'], entry['title'], new_name))

            if main_file is not None:
                # proceed with renaming only if such a big file is found

                # find the subtitle file
                keep_subs = opts.get('keep_subs')
                sub_file = None
                if keep_subs:
                    sub_exts = [".srt", ".sub"]
                    for file in status['files']:
                        ext = os.path.splitext(file['path'])[1]
                        if ext in sub_exts:
                            sub_file = file
                            break

                # check for single file torrents so we dont add unnecessary folders
                if (os.path.dirname(main_file['path']) is not ("" or "/")):
                    # check for top folder in user config
                    if (opts.get('content_filename') and os.path.dirname(opts['content_filename']) is not ""):
                        top_files_dir = os.path.dirname(opts['content_filename']) + "/"
                    else:
                        top_files_dir = os.path.dirname(main_file['path']) + "/"
                else:
                    top_files_dir = "/"

                if opts.get('content_filename'):
                    # rename the main file
                    big_file_name = (top_files_dir +
                                     os.path.basename(opts['content_filename']) +
                                     os.path.splitext(main_file['path'])[1])
                    big_file_name = unused_name(big_file_name)
                    rename(main_file, big_file_name)

                    # rename subs along with the main file
                    if sub_file is not None and keep_subs:
                        sub_file_name = (os.path.splitext(big_file_name)[0] +
                                         os.path.splitext(sub_file['path'])[1])
                        rename(sub_file, sub_file_name)

                if opts.get('main_file_only'):
                    # download only the main file (and subs)
                    file_priorities = [1 if f == main_file or (f == sub_file and keep_subs) else 0
                                       for f in status['files']]
                    self.client.call('core.set_torrent_file_priorities', torrent_id, file_priorities)

                    if opts.get('hide_sparse_files'):
                        # hide the other sparse files that are not supposed to download but are created anyway
                        # http://dev.deluge-torrent.org/ticket/1827
                        # Made sparse files behave better with deluge http://flexget.com/ticket/2881
                        sparse_files = [f for f in status['files']
                                        if f != main_file and (f != sub_file or (not keep_subs))]
                        rename_pairs = [(f['index'],
                                         top_files_dir + ".sparse_files/" + os.path.basename(f['path']))
                                        for f in sparse_files]
                        self.client.call('core.rename_files', torrent_id, rename_pairs)
            else:
                log.warning('No files in "%s" are > %d%% of content size, no files renamed.' % (
                    entry['title'],
                    opts.get('main_file_ratio') * 100))

        container_directory =  pathscrub(entry.render(entry.get('container_directory', opts.get('container_directory', ''))))
        if container_directory:
            if big_file_name:
                folder_structure = big_file_name.split(os.sep)
            elif len(status['files']) > 0:
                folder_structure = status['files'][0]['path'].split(os.sep)
            else:
                folder_structure = []
            if len(folder_structure) > 1:
                log.verbose('Renaming Folder %s to %s', folder_structure[0], container_directory)
                self.client.call('core.rename_folder', torrent_id, folder_structure[0], container_directory)
            else:
                log.debug('container_directory specified however the torrent %s does not have a directory structure; skipping folder rename', entry['title'])


@event('plugin.register')
def register_plugin():
    plugin.register(InputDeluge, 'from_deluge3', api_ver=2)
    plugin.register(OutputDeluge, 'deluge3', api_ver=2)