# pylint: disable=too-many-arguments,too-many-instance-attributes

"""This module contains modules for saving data to a database"""


from __future__ import unicode_literals, division, print_function
import re
import time
import logging
import threading
from collections import namedtuple
# Py2/3 import of Queue
try:
    from Queue import Queue
except ImportError:
    from queue import Queue  # pylint: disable=import-error

import MySQLdb

# Used for check of valid, un-escaped column names, to prevent injection
COLUMN_NAME = re.compile(r'^[0-9a-zA-Z$_]*$')


# namedtuple used for custom column formatting, see MeasurementSaver.__init__
CustomColumn = namedtuple('CustomColumn', ['value', 'format_string'])


# Loging object for the DataSetSaver (DSS) shortened, because it will be written a lot
DSS_LOG = logging.getLogger(__name__ + '.MeasurementSaver')
DSS_LOG.addHandler(logging.NullHandler())


class DataSetSaver(object):
    """A class to save a measurement"""

    host = 'servcinf'
    database = 'cinfdata'

    def __init__(self, measurements_table, xy_values_table, username, password,
                 measurement_specs=None):
        """Initialize local parameters

        Args:
            measurements_table (str): The name of the measurements table
            xy_values_table (str): The name of the xy values table
            username (str): The database username
            passwork (str): The database password
            measurement_specs (sequence): A sequence of ``measurement_codename,
                metadata`` pairs, see below

        ``measurement_specs`` is used if you want to initialize all the measurements at
        ``__init__`` time. You can also do it later with :meth:`add_measurement`. The
        expected value is a sequence of ``measurement_codename, metadata`` e.g::

            measurement_specs = [
                ['M2', {'type': 5, 'timestep': 0.1, 'mass_label': 'M2M'}],
                ['M28', {'type': 5, 'timestep': 0.1, 'mass_label': 'M2M'}],
                ['M32', {'type': 5, 'timestep': 0.1, 'mass_label': 'M2M'}],
            ]

        As above, the expected ``metadata`` is simply a mapping of column names to column
        values in the ``measurements_table``.

        Per default, the value will be put into the table as is. If it is necessary to do
        SQL processing on the value, to make it fit the column type, then the value must
        be replaced with a :class:`CustomColumn` instance, whose arguments are the value
        and the format/processing string. The format/processing string must contain a '%s'
        as a placeholder for the value. It could look like this::

            measurement_specs = [
                ['M2', {'type': 5, 'time': CustomColumn(M2_timestamp, 'FROM_UNIXTIME(%s)')}],
                ['M28', {'type': 5, 'time': CustomColumn(M28_timestamp, 'FROM_UNIXTIME(%s)')}],
            ]

        The most common use for this is the one shown above, where the ``time`` column is
        of type timestamp and the time value (e.g. in M2_timestamp) is a unix
        timestamp. The unix timestamp is converted to a SQL timestamp with the
        ``FROM_UNIXTIME`` SQL function.

        .. note:: The SQL timestamp column understand the :py:class:`datetime.datetime`
            type directly, so if the input timestamp is already on that form, then there
            is no need to convert it

        """
        DSS_LOG.info(
            '__init__ with measurement_table=%s, xy_values_table=%s, '
            'username=%s, password=*****, measurement_specs: %s',
            measurements_table, xy_values_table, username, measurement_specs,
        )

        # Initialize instance variables
        self.measurements_table = measurements_table
        self.xy_values_table = xy_values_table
        self.sql_saver = SqlSaver(username, password)
        self.sql_saver.start()

        # Initialize queries
        self.insert_measurement_query = 'INSERT INTO {} ({{}}) values ({{}})'\
            .format(measurements_table)
        self.insert_point_query = 'INSERT INTO {} (measurement, x, y) values (%s, %s, %s)'\
            .format(xy_values_table)
        self.insert_batch_query = 'INSERT INTO {} (measurement, x, y) values {{}}'\
            .format(xy_values_table)

        # Initialize measurement ids
        self.measurement_ids = {}
        if measurement_specs:
            for codename, metadata_dict in measurement_specs:
                self.add_measurement(codename, metadata_dict)

    def add_measurement(self, codename, metadata):
        """Add a measurement

        This is equivalent to forming the entry in the measurements table with the
        metadata values and saving the id of this entry locally for use with
        :meth:`add_point`.

        Args:
            codename (str): The codename that this measurement should have
            metadata (dict): The dictionary that holds the information for the
                measurements table. See :meth:`__init__` for details.
        """
        DSS_LOG.info('Add measurement codenamed: \'%s\' with metadata: %s', codename, metadata)
        # Collect column names, values and format strings, a format string is a SQL value
        # placeholder including processing like e.g: %s or FROM_UNIXTIME(%s)
        column_names, values, value_format_strings = [], [], []
        for column_name, value in metadata.items():
            if not COLUMN_NAME.match(column_name):
                message = 'Invalid column name: \'{}\'. Only column names using a-z, '\
                          'A-Z, 0-9 and \'_\' and \'$\' are allowed'.format(column_name)
                raise ValueError(message)

            if isinstance(value, CustomColumn):
                # If the value from the metadata dict is a CustomColumn (which is 2 value
                # tuple), the unpack it
                real_value, value_format_string = value
            else:
                # Else, that value is just a value with default place holder
                real_value, value_format_string = value, '%s'

            column_names.append(column_name)
            values.append(real_value)
            value_format_strings.append(value_format_string)

        # Form the column string e.g: 'name, time, type'
        column_string = ', '.join(column_names)
        # Form the value marker string e.g: '%s, FROM_UNIXTIME(%s), %s'
        value_marker_string = ', '.join(value_format_strings)
        query = self.insert_measurement_query.format(column_string, value_marker_string)

        # Make the insert and save the measurement_table id for use in saving the data
        cursor = self.sql_saver.cnxn.cursor()
        cursor.execute(query, values)
        self.measurement_ids[codename] = cursor.lastrowid
        cursor.close()
        DSS_LOG.debug('Measurement codenamed: \'%s\' added', codename)

    def save_point(self, codename, point):
        """Save a point for a specific codename

        Args:
            codename (str): The codename for the measurement to add the point to
            point (sequence): A sequence of x, y
        """
        DSS_LOG.debug('For codename \'%s\' save point: %s', codename, point)
        try:
            query_args = [self.measurement_ids[codename]]
        except KeyError:
            message = 'No entry in measurements_ids for codename: \'{}\''.format(codename)
            raise ValueError(message)

        # The query expects 3 values; measurement_id, x, y
        query_args.extend(point)
        self.sql_saver.enqueue_query(self.insert_point_query, query_args)

    def save_points_batch(self, codename, x_values, y_values, batchsize=100):
        """Save a number points for the same codename in batches

        Args:
            codename (str): The codename for the measurement to save the points for
            x_values (sequence): A sequence of x values
            y_values (sequence): A sequence of y values
            batchsize (int): The number of points to send in the same batch
        """
        DSS_LOG.debug('For codename \'%s\' save %s points in batches of %s',
                      codename, len(x_values), batchsize)

        # Check lengths and get measurement_id
        if len(x_values) != len(y_values):
            raise ValueError('Number of x and y values must be the same. Values are {} and {}'\
                             .format(len(x_values), len(y_values)))
        try:
            measurement_id = self.measurement_ids[codename]
        except KeyError:
            message = 'No entry in measurements_ids for codename: \'{}\''.format(codename)
            raise ValueError(message)

        # Gather values in batches of batsize, start enumerate from 1, to make the criteria
        values = []
        number_of_values = 0
        for x_value, y_value in zip(x_values, y_values):
            # Add this point
            values.extend([measurement_id, x_value, y_value])
            number_of_values += 1

            # Save a batch (> should not be necessary)
            if number_of_values >= batchsize:
                value_marker_string = ', '.join(['(%s, %s, %s)'] * number_of_values)
                query = self.insert_batch_query.format(value_marker_string)
                self.sql_saver.enqueue_query(query, values)
                values = []
                number_of_values = 0

        # Save the remaining number of points (smaller than the batchsize)
        if number_of_values > 0:
            value_marker_string = ', '.join(['(%s, %s, %s)'] * number_of_values)
            query = self.insert_batch_query.format(value_marker_string)
            self.sql_saver.enqueue_query(query, values)

    def stop(self):
        """Stop the MeasurementSaver

        And shut down the underlying :class:`PyExpLabSys.common.sql_saver.SqlSaver`
        instance nicely.
        """
        DSS_LOG.info('stop called')
        self.sql_saver.stop()
        DSS_LOG.debug('stopped')

    @property
    def connection(self):
        """Return the connection of the underlying SqlSaver instance"""
        return self.sql_saver.cnxn


CDS_LOG = logging.getLogger(__name__ + '.ContinuousDataSaver')
CDS_LOG.addHandler(logging.NullHandler())


class ContinuousDataSaver(object):
    """This class saves data to the database for continuous measurements

    Continuous measurements are measurements of a single parameters as a function of
    datetime. The class can ONLY be used with the new layout of tables for continous data,
    where there is only one table per setup, as apposed to the old layout where there was
    one table per measurement type per setup. The class sends data to the ``cinfdata``
    database at host ``servcinf``.

    :var host: Database host, value is ``servcinf``.
    :var database: Database name, value is ``cinfdata``.

    """

    host = 'servcinf'
    database = 'cinfdata'

    def __init__(self, continuous_data_table, username, password, measurement_codenames=None):
        """Initialize the continous logger

        Args:
            continuous_data_table (str): The contunuous data table to log data to
            username (str): The MySQL username
            password (str): The password for ``username`` in the database
            measurement_codenames (sequence): A sequence of measurement codenames that this
                logger will send data to. These codenames can be given here, to initialize
                them at the time of initialization of later by the use of the
                :meth:`add_continuous_measurement` method.

        .. note:: The codenames are the 'official' codenames defined in the database for
            contionuous measurements NOT codenames that can be userdefined

        """
        CDS_LOG.info(
            '__init__ with continuous_data_table=%s, username=%s, password=*****, '
            'measurement_codenames=%s', continuous_data_table, username, measurement_codenames,
        )

        # Initialize instance variables
        self.continuous_data_table = continuous_data_table
        self.sql_saver = SqlSaver(username, password)
        self.sql_saver.start()
        self.username = username
        self.password = password

        # Dict used to translate code_names to measurement numbers
        self.codename_translation = {}
        if measurement_codenames is not None:
            for codename in measurement_codenames:
                self.add_continuous_measurement(codename)

    def add_continuous_measurement(self, codename):
        """Add a continuous measurement codename to this saver

        Args:
            codename (str): Codename for the measurement to add

        .. note:: The codenames are the 'official' codenames defined in the database for
            contionuous measurements NOT codenames that can be userdefined

        """
        CDS_LOG.info('Add measurements for codename \'%s\'', codename)
        query = 'SELECT id FROM dateplots_descriptions WHERE codename=\'{}\''.format(codename)
        cursor = self.connection.cursor()
        cursor.execute(query)
        results = cursor.fetchall()
        cursor.close()
        if len(results) != 1:
            message = 'Measurement code name \'{}\' does not have exactly one entry in '\
                      'dateplots_descriptions'.format(codename)
            CDS_LOG.critical(message)
            raise ValueError(message)
        self.codename_translation[codename] = results[0][0]

    def save_point_now(self, codename, value):
        """Save a value and use now (a call to :func:`time.time`) as the timestamp

        Args:
            codename (str): The measurement codename that this point will be saved under
            value (float): The value to be logged

        Returns:
            float: The Unixtime used
        """
        unixtime = time.time()
        CDS_LOG.debug('Adding timestamp %s to value %s for codename %s', unixtime, value,
                      codename)
        self.save_point(codename, (unixtime, value))
        return unixtime

    def save_point(self, codename, point):
        """Save a point

        Args:
            codename (str): The measurement codename that this point will be saved under
            point (sequence): The point to be saved, as a sequence of 2 floats: (x, y)
        """
        try:
            unixtime, value = point
        except ValueError:
            message = '\'point\' must be a iterable with 2 values, got {}'.format(point)
            raise ValueError(message)

        # Save the point
        CDS_LOG.debug('Save point (%s, %s) for codename: %s', unixtime, value, codename)
        measurement_number = self.codename_translation[codename]
        query = ('INSERT INTO {} (type, time, value) VALUES (%s, FROM_UNIXTIME(%s), %s);')
        query = query.format(self.continuous_data_table)
        self.sql_saver.enqueue_query(query, (measurement_number, unixtime, value))

    def stop(self):
        """Stop the ContiniousDataSaver

        And shut down the underlying :class:`PyExpLabSys.common.sql_saver.SqlSaver`
        instance nicely.
        """
        CDS_LOG.info('stop called')
        self.sql_saver.stop()
        CDS_LOG.debug('stop finished')

    @property
    def connection(self):
        """Return the connection of the underlying SqlSaver instance"""
        return self.sql_saver.cnxn


SQL_SAVER_LOG = logging.getLogger(__name__ + '.SqlSaver')
SQL_SAVER_LOG.addHandler(logging.NullHandler())


class SqlSaver(threading.Thread):
    """The SqlSaver class administers a queue from which it makes the SQL inserts

    Attributes:
        queue (Queue.queue): The queue the queries and qeury arguments are stored in. See
            note below.
        commits (int): The number of commits the saver has performed
        commit_time (float): The timespan the last commit took
        cnxn (MySQLdb.connection): The MySQLdb database connection
        cursor (MySQLdb.cursor): The MySQLdb database cursor

    .. note:: In general queries are added to the queue via the
        :meth:enqueue_query`` method. If it is desired to add elements manually, remember
        that they must be on the form of a ``(query, query_args)`` tuple. (These are the
        arguments to the execute method on the cursor object)

    """

    hostname = 'servcinf'
    database = 'cinfdata'

    def __init__(self, username, password, queue=None):
        """Initialize local variables

        Args:
            username (str): The username for the MySQL database
            password (str): The password for the MySQL database
            queue (Queue.queue): A custom queue to use. If it is left out, a new
                :py:class:`Queue.queue` object will be used.
        """

        SQL_SAVER_LOG.info('Init with username: %s, password: ***** and queue: %s',
                           username, queue)
        super(SqlSaver, self).__init__()
        #threading.Thread.__init__(self)
        self.daemon = True

        # Initialize internal variables
        self.username = username
        self.password = password
        self.commits = 0
        self.commit_time = 0

        # Set queue or initialize a new one
        if queue is None:
            self.queue = Queue()
        else:
            self.queue = queue

        # Initialize database connection
        SQL_SAVER_LOG.debug('Open connection to MySQL database')
        self.cnxn = MySQLdb.connect(host=self.hostname, user=username,
                                    passwd=password, db=self.database)
        self.cursor = self.cnxn.cursor()
        SQL_SAVER_LOG.debug('Connection opened, init done')

    def stop(self):
        """Add stop word to queue to exit the loop when the queue is empty"""
        SQL_SAVER_LOG.info('stop called')
        self.queue.put(('STOP', None))
        # Make sure to wait untill it is closed down to return, otherwise we are going to
        # tear down the environment around it
        while self.isAlive():
            time.sleep(10**-5)
        SQL_SAVER_LOG.debug('stopped')

    def enqueue_query(self, query, query_args=None):
        """Enqueue a qeury and arguments

        Args:
            query (str): The SQL query to be executed
            query_args (sequence or mapping): Optional sequence or mapping of arguments
                to be formatted into the query. ``query`` and ``query_args`` in combination
                are the arguments to cursor.execute.
        """
        SQL_SAVER_LOG.debug('Enqueue query \'%s\' with args: %s', query, query_args)
        self.queue.put((query, query_args))

    def run(self):
        """Execute SQL inserts from the queue untill stopped"""
        SQL_SAVER_LOG.info('run started')
        while True:
            start = time.time()
            query, args = self.queue.get()
            if query == 'STOP': # Magic key-word to stop Sql Saver
                break
            success = False
            while not success:
                try:
                    self.cursor.execute(query, args=args)
                    success = True
                except MySQLdb.OperationalError: # Failed to perfom commit
                    time.sleep(5)
                    try:
                        self.cnxn = MySQLdb.connect(host=self.hostname, user=self.username,
                                                    passwd=self.password, db=self.database)
                        self.cursor = self.cnxn.cursor()
                    except MySQLdb.OperationalError: # Failed to re-connect
                        pass

            self.cnxn.commit()
            self.commits += 1
            self.commit_time = time.time() - start

        self.cnxn.close()
        SQL_SAVER_LOG.debug('run stopped')