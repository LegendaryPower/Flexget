"""
NOTE:

Avoid using this module on your own or in plugins, this was originally made for 0.9 -> 1.0 transition.

You can safely use task.simple_persistence and manager.persist, if we implement something better we
can replace underlying mechanism in single point (and provide transparent switch).
"""

from __future__ import unicode_literals, division, absolute_import
from collections import MutableMapping, defaultdict
from datetime import datetime
import logging
import pickle

from sqlalchemy import Column, Integer, String, DateTime, PickleType, select, Index

from flexget import db_schema
from flexget.manager import Session
from flexget.utils.database import safe_pickle_synonym
from flexget.utils.sqlalchemy_utils import table_schema, create_index

log = logging.getLogger('util.simple_persistence')
Base = db_schema.versioned_base('simple_persistence', 2)

# Used to signify that a given key should be deleted from simple persistence on flush
DELETE = object()


@db_schema.upgrade('simple_persistence')
def upgrade(ver, session):
    if ver is None:
        # Upgrade to version 0 was a failed attempt at cleaning bad entries from our table, better attempt in ver 1
        ver = 0
    if ver == 0:
        # Remove any values that are not loadable.
        table = table_schema('simple_persistence', session)
        for row in session.execute(select([table.c.id, table.c.plugin, table.c.key, table.c.value])):
            try:
                p = pickle.loads(row['value'])
            except Exception as e:
                log.warning('Couldn\'t load %s:%s removing from db: %s' % (row['plugin'], row['key'], e))
                session.execute(table.delete().where(table.c.id == row['id']))
        ver = 1
    if ver == 1:
        log.info('Creating index on simple_persistence table.')
        create_index('simple_persistence', session, 'feed', 'plugin', 'key')
        ver = 2
    return ver


class SimpleKeyValue(Base):
    """Declarative"""

    __tablename__ = 'simple_persistence'

    id = Column(Integer, primary_key=True)
    task = Column('feed', String)
    plugin = Column(String)
    key = Column(String)
    _value = Column('value', PickleType)
    value = safe_pickle_synonym('_value')
    added = Column(DateTime, default=datetime.now())

    def __init__(self, task, plugin, key, value):
        self.task = task
        self.plugin = plugin
        self.key = key
        self.value = value

    def __repr__(self):
        return "<SimpleKeyValue('%s','%s','%s')>" % (self.task, self.key, self.value)

Index('ix_simple_persistence_feed_plugin_key', SimpleKeyValue.task, SimpleKeyValue.plugin, SimpleKeyValue.key)


class SimplePersistence(MutableMapping):
    # Stores values in store[taskname][pluginname][key] format
    class_store = defaultdict(defaultdict(dict))

    def __init__(self, plugin=None):
        self.taskname = None
        self.plugin = plugin

    @property
    def store(self):
        return self.class_store[self.taskname][self.plugin]

    def __setitem__(self, key, value):
        log.debug('setting key %s value %s' % (key, repr(value)))
        self.store[key] = value

    def __getitem__(self, key):
        if 'key' in self.store:
            if self.store[key] == DELETE:
                raise KeyError('%s is not contained in the simple_persistence table.' % key)
            return self.store[key]

        with Session() as session:
            skv = session.query(SimpleKeyValue).filter(SimpleKeyValue.task == self.taskname).\
                filter(SimpleKeyValue.plugin == self.plugin).filter(SimpleKeyValue.key == key).first()
            if not skv:
                raise KeyError('%s is not contained in the simple_persistence table.' % key)
            else:
                self.store[key] = skv.value
                return skv.value

    def __delitem__(self, key):
        self.store[key] = DELETE

    def __iter__(self):
        raise NotImplementedError('simple persistence does not support iteration')

    def __len__(self):
        raise NotImplementedError('simple persistence does not support `len`')

    def flush(self):
        """Flush all in memory key/values to database."""
        log.debug('Flushing simple persistence updates to db.')
        # TODO: the stuff

class SimpleTaskPersistence(SimplePersistence):

    def __init__(self, task):
        self.task = task
        self.taskname = task.name

    @property
    def plugin(self):
        return self.task.current_plugin
