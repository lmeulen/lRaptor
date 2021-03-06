import os
import io
import sys
import pandas as pd
import numpy as np
import time
import logging
import argparse
import pickle
from datetime import datetime
from dataclasses import dataclass

# Default transfer time is 3 minutes
TRANSFER_COST = (3 * 60)
SAVE_RESULTS = False
T24H = 24 * 60 * 60
T6H = 6 * 60 * 60
T1H = 1 * 60 * 60
T3M = 3 * 60

# create logger
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(ch)


@dataclass
class Timetable:
    agencies = None
    routes = None
    trips = None
    calendar = None
    stop_times = None
    stop_times_filtered = None
    stops = None
    station2stops = None
    stop_times_for_trips = None
    transfers = None
    stops_array = None
    s2s_indexer = None
    s2s_data = None
    disruptions = None


class Namespace:
    """
    Namespace class for dummy arguments
    """
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


timetable = Timetable()
evaluations = []
k = 0


def str2sec(time_str):
    """
    Convert hh:mm:ss to seconds since midnight
    :param time_str: String in format hh:mm:ss
    """
    spl = time_str.strip().split(":")
    if len(spl) == 3:
        h, m, s = spl
        return int(h) * 3600 + int(m) * 60 + int(s)
    m, s = spl
    return int(m) * 60 + int(s)


def sec2str(scnds, show_sec=False):
    """
    Convert hh:mm:ss to seconds since midnight
    :param show_sec: only show :ss if True
    :param scnds: Seconds to translate to hh:mm:ss
    """
    h = int(scnds / 3600)
    m = int((scnds % 3600) / 60)
    s = int(scnds % 60)
    return "{:02d}:{:02d}:{:02d}".format(h, m, s) if show_sec else "{:02d}:{:02d}".format(h, m)


def str2bool(v):
    """
    Convert a string to bool, function for the argument parser
    :param v: string representing true or false value in any format
    :return: bool
    """
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


def read_timetable(gtfs_dir):
    """
    Read the timetable information from either cache or GTFS directory
    Global parameter READ_GTFS determines cache or reading original GTFS files
    :return:
    """
    global timetable
    logger.debug('Reading GTFS data')
    timetable.agencies = pd.read_csv(os.path.join(gtfs_dir, 'agency.txt'))

    timetable.routes = pd.read_csv(os.path.join(gtfs_dir, 'routes.txt'))

    timetable.trips = pd.read_csv(os.path.join(gtfs_dir, 'trips.txt'))
    timetable.trips.trip_short_name = timetable.trips.trip_short_name.astype(int)
    timetable.trips.shape_id = timetable.trips.shape_id.astype('Int64')

    timetable.calendar = pd.read_csv(os.path.join(gtfs_dir, 'calendar_dates.txt'))
    timetable.calendar.date = timetable.calendar.date.astype(str)

    timetable.stop_times = pd.read_csv(os.path.join(gtfs_dir, 'stop_times.txt'))
    timetable.stop_times.arrival_time = timetable.stop_times.apply(lambda x: str2sec(x['arrival_time']), axis=1)
    timetable.stop_times.departure_time = timetable.stop_times.apply(lambda x: str2sec(x['departure_time']),
                                                                     axis=1)
    timetable.stop_times.stop_id = timetable.stop_times.stop_id.astype(str)
    timetable.stop_times_filtered = None

    timetable.stops = pd.read_csv(os.path.join(gtfs_dir, 'stops.txt'))
    # Filter out the general station codes
    timetable.stops = timetable.stops[~timetable.stops.stop_id.str.startswith('stoparea')]

    logger.debug('Agencies  : {}'.format(len(timetable.agencies)))
    logger.debug('Routes    : {}'.format(len(timetable.routes)))
    logger.debug('Stops     : {}'.format(len(timetable.stops)))
    logger.debug('Trips     : {}'.format(len(timetable.trips)))
    logger.debug('Stoptimes : {}'.format(len(timetable.stop_times)))


def get_trip_ids_for_stop(stop_id, dep_time, forward=T6H, tripfilter=None):
    """Takes a stop and departure time and get associated trip ids.
       The forward parameter limits the time frame starting at the departure time.
       Default framesize is 60 minutes
       Times asre specified in seconds sincs midnight
    :param stop_id: Stop
    :param dep_time: Departure time
    :param forward: Period forward limitimg trips
    :param tripfilter: If specified contains tripnumbers to exclude
    """
    global timetable
    mask_1 = timetable.stop_times_filtered.index == stop_id
    mask_2 = timetable.stop_times_filtered.departure_time.between(dep_time, dep_time + forward)
    mask_3 = True
    if tripfilter:
        mask_3 = ~timetable.stop_times_filtered.trip_id.isin(tripfilter)
    # extract the list of qualifying trip ids
    potential_trips = timetable.stop_times_filtered[mask_1 & mask_2 & mask_3].trip_id.tolist()
    return potential_trips


def traverse_trips(ids, bag, departure_time, filter_trips):
    """ Iterator through the stops reachable and add all new reachable stops
        by following all trips from the reached stations. Trips are only followed
        in the direction of travel and beyond already added points
    :param ids: Current stops reached
    :param bag: numpy array with info over reached stops
    :param departure_time: Departure time
    :param filter_trips: trips to filter from the list of potential trips
    """
    global timetable
    # prevent upstream mutation of dictionary
    new_stops = []

    i = 0
    j = 0
    for start_stop in ids:
        # how long it took to get to the stop so far (0 for start node)
        time_sofar = bag[start_stop][0]
        # get list of all trips associated with this stop
        trips = get_trip_ids_for_stop(start_stop, departure_time + time_sofar, T1H, filter_trips)
        filter_trips.extend(trips)
        for trip in trips:

            # get all the stop time arrivals for that trip
            stop_times = timetable.stop_times_for_trips[timetable.stop_times_for_trips.index == trip]

            # get the point where we join this specific trip
            from_here = stop_times[stop_times.stop_id == start_stop].iloc[0]['stop_sequence']
            # get all following stops
            stop_times = stop_times[(stop_times.stop_sequence > from_here)]

            # for all following stops, calculate time to reach
            arrivals = zip(stop_times.arrival_time, stop_times.stop_id)
            for arrive_time, arrive_stop_id in arrivals:
                i += 1
                if SAVE_RESULTS:
                    evaluations.append((k, start_stop, trip,arrive_stop_id, arrive_time))
                # time to reach is diff from start time to arrival (plus any baseline cost)
                arrive_time_adjusted = arrive_time - departure_time

                # only update if does not exist yet or is faster
                old_value = bag[arrive_stop_id][0]
                if arrive_time_adjusted < old_value:
                    j += 1
                    bag[arrive_stop_id] = (arrive_time_adjusted, trip, start_stop)
                    new_stops.append(arrive_stop_id)

    logger.debug('         Evaluations    : {}'.format(i))
    logger.debug('         Improvements   : {}'.format(j))
    filter_trips = list(set(filter_trips))
    return new_stops


def get_transfer_time(stop_from, stop_to, timesec, dow):
    """
    Calculate the transfer time from a stop to another stop (usually two platforms at one station
    :param stop_from: Origin platform
    :param stop_to: Destination platform
    :param timesec: Time of day (seconds since midnight)
    :param dow: day of week (Monday = 0, Tuesday = 1, ...)
    :return:
    """
    return TRANSFER_COST


def add_transfer_time(ids, bag):
    """
    Add transfers between platforms
    :param ids:
    :param bag:
    :return:
    """
    global timetable
    # prevent upstream mutation of dictionary
    new_stops = []

    # add in transfers to other platforms
    for stop in ids:
        # stopdate is numpy array with index stopid and tuple (name, station, platform,transfer)
        stopdata = timetable.stops_array[stop]
        stoparea = stopdata[1]

        # Only add transfers if it is a transfer station
        if stopdata[3]:
            # only update if currently inaccessible or faster than currrent option
            stopidx = timetable.s2s_indexer[stoparea]
            for arrive_stop_id in timetable.s2s_data[stopidx[0]:stopidx[0]+stopidx[1]]:
                # time to reach new nearby stops is the transfer cost plus arrival at last stop
                time_sofar = bag[stop][0]
                arrive_time_adjusted = time_sofar + get_transfer_time(stop, arrive_stop_id, time_sofar, 0)
                old_value = bag[arrive_stop_id][0]
                if arrive_time_adjusted < old_value:
                    bag[arrive_stop_id] = (arrive_time_adjusted, 0, stop)
                    new_stops.append(arrive_stop_id)

    return new_stops


def determine_parameters(start_name, end_name, departure_time):
    """ Determine algorithm paramters based upon human readable information
        start_name : Location to start journey
        end_name: Endpoint of the journey
        departure_time: Time of the departure in hh:mm:ss
    """
    global timetable
    # look at all trips from that stop that are after the depart time
    departure = str2sec(departure_time)

    # get all information, including the stop ids, for the start and end nodes
    from_loc = timetable.stops[timetable.stops.stop_name == start_name].index.to_list()
    to_loc = timetable.stops[timetable.stops.stop_name == end_name].index.to_list()

    return from_loc, to_loc, departure


def final_destination(to_ids, bag):
    """ Find the destination ID with the shortest distance
        Required in order to prevent adding travel time to the arrival time
    :param to_ids:
    :param bag:
    :return:
    """
    final_id = 0
    distance = T24H
    for to_id in to_ids:
        if bag[to_id][0] < distance:
            distance = bag[to_id][0]
            final_id = to_id
    return final_id


def prepare_data_for_run(arrival_name, departure_name, departure_date, departure_time):
    from_stops, to_stops, dep_secs = determine_parameters(departure_name, arrival_name, departure_time)
    logger.debug('Departure ID : ' + str(from_stops))
    logger.debug('Arrival ID   : ' + str(to_stops))
    logger.debug('Time         : ' + str(dep_secs))

    # Filter timetable stop times, keep only coming 6 hours on date of departure
    trips = timetable.trips[timetable.trips.date == departure_date]['trip_id'].values
    mask1 = timetable.stop_times.departure_time.between(dep_secs, dep_secs + T6H)
    mask2 = timetable.stop_times.trip_id.isin(trips)

    timetable.stop_times_filtered = timetable.stop_times[mask1 & mask2].copy()
    return dep_secs, from_stops, to_stops


def perform_lraptor(departure_name, arrival_name, departure_date, departure_time, iterations, disruptions=False):
    """
    Perform the Raptor algorithm
    :param departure_name: Name of departure location
    :param arrival_name: Name of arrival location
    :param departure_date: Date of departure, str format (yyyymmhh)
    :param departure_time: Time of departure, str format (hh:mm:sss)
    :param iterations: Number of iterations to perform
    :param disruptions: Take disruptions into account (True)
    :return:
    """
    global timetable, k
    # Determine start and stop area
    from_stops, to_stops, dep_secs = determine_parameters(departure_name, arrival_name, departure_time)
    logger.debug('Departure ID : ' + str(from_stops))
    logger.debug('Arrival ID   : ' + str(to_stops))
    logger.debug('Time         : ' + str(dep_secs))

    # initialize lookup with start node taking 0 seconds to reach
    k_results = {}
    numberstops = max(timetable.stops.index)+1
    # bag contains per stop (travel_time, trip_id, previous_stop) trip_id is 0 in case of a transfer
    bag = np.full(shape=(numberstops, 3), fill_value=(T24H, 0, -1), dtype=np.dtype(np.int32, np.int32, np.int32))
    new_stops = []
    tripfilter = []
    # Filter timetable stop times, keep only
    # - coming 6 hours
    # - date of departure
    # non disrupted trip ids
    trips = timetable.trips[timetable.trips.date == departure_date]['trip_id'].values
    mask1 = timetable.stop_times.departure_time.between(dep_secs, dep_secs + T6H)
    mask2 = timetable.stop_times.trip_id.isin(trips)
    mask3 = True
    if disruptions:
        mask3 = ~timetable.stop_times.trip_id.isin(timetable.disruptions)
    timetable.stop_times_filtered = timetable.stop_times[mask1 & mask2 & mask3].copy()

    for from_stop in from_stops:
        bag[from_stop] = (0, 0, 0)
        new_stops.append(from_stop)
    logger.debug('Starting from IDS : '.format(str(from_stops)))

    for k in range(1, iterations + 1):
        logger.info("Analyzing possibilities round {}".format(k))

        # get list of stops to evaluate in the process
        logger.debug("    Stops to evaluate count: {}".format(len(new_stops)))

        # update time to stops calculated based on stops accessible
        t = time.perf_counter()
        new_stops_travel = traverse_trips(new_stops, bag, dep_secs, tripfilter)
        logger.debug("    Travel stops  calculated in {:0.4f} seconds".format(time.perf_counter() - t))
        logger.debug("    {} stops added".format(len(new_stops_travel)))

        # now add footpath transfers and update
        t = time.perf_counter()
        new_stops_transfer = add_transfer_time(new_stops_travel, bag)
        logger.debug("    Transfers calculated in {:0.4f} seconds".format(time.perf_counter() - t))
        logger.debug("    {} stops added".format(len(new_stops_transfer)))

        new_stops = set(new_stops_travel).union(new_stops_transfer)
        logger.debug("    {} stops to evaluate in next round".format(len(new_stops)))

        # Store the results for this round
        k_results[k] = np.copy(bag)
        mask = ~timetable.stop_times_filtered.trip_id.isin(tripfilter)
        timetable.stop_times_filtered = timetable.stop_times_filtered[mask]
    # Determine the best destionation ID, destination is a platform.
    dest_id = final_destination(to_stops, bag)
    if dest_id != 0:
        logger.debug("Destination code   : {} ".format(dest_id))
        logger.info("Time to destination: {} minutes".format(bag[dest_id][0] / 60))
    else:
        logger.info("Destination unreachable with given parameters")
    return k_results, dest_id, bag


def export_results(traveltimes, bag):
    """
    Export results to a CSV file with stations and traveltimes (per iteration)
    :param traveltimes: datastructure with results per iteration
    :param bag: Final destination last leg
    :return: DataFrame with the results exported
    """
    global evaluations
    filename1 = 'res_{date:%Y%m%d_%H%M%S}_traveltime.csv'.format(date=datetime.now())
    logger.debug('Export results to {}'.format(filename1))
    datastring = 'round,stop_id,stop_name,platform_code,travel_time\n'
    for i in list(traveltimes.keys()):
        locations = traveltimes[i]
        destination = 0
        for tt in locations:
            stop = timetable.stops[timetable.stops.index == destination]
            if (not stop.empty) and destination < T24H:
                name = stop['stop_name'].values[0]
                platform = stop['platform_code'].values[0]
                datastring += (str(i) + ',' + str(destination) + ',' + str(name) + ',' + str(platform) + ',' +
                               str(tt[0]) + '\n')
            destination = destination + 1
    df = pd.read_csv(io.StringIO(datastring), sep=",")
    df = df[['round', 'stop_name', 'travel_time']].groupby(['round', 'stop_name']).min().sort_values('travel_time')
    df.travel_time = df.apply(lambda x: sec2str(x.travel_time), axis=1)
    df.to_csv(filename1)

    filename2 = 'res_{date:%Y%m%d_%H%M%S}_last_legs.csv'.format(date=datetime.now())
    logger.debug('Export results to {}'.format(filename2))
    datastring = 'from_id,trip_id,stop_id\n'
    for b in bag:
        frm = b[0]
        via = b[1]
        to = b[2]
        datastring += (str(frm) + ',' + str(via) + ',' + str(to) + '\n')
    df2 = pd.read_csv(io.StringIO(datastring), sep=",")
    df2.to_csv(filename2)

    filename3 = 'res_{date:%Y%m%d_%H%M%S}_evaluations.csv'.format(date=datetime.now())
    df3 = pd.DataFrame(evaluations, columns=['k', 'from', 'trip', 'to', 'arrival'])
    df3 = df3.merge(timetable.stops[['stop_name', 'platform_code']], left_on='from', right_index=True)
    df3 = df3.merge(timetable.trips, left_on='trip', right_on='trip_id')
    df3 = df3.merge(timetable.stops[['stop_name', 'platform_code']], left_on='to', right_index=True)
    df3 = df3.drop(['date', 'service_id'], axis=1).drop_duplicates()
    df3.columns = ['k', 'from', 'trip', 'to', 'arrival', 'from_name', 'from_platform', 'trip_id', 'trip_number',
                   'to_name', 'to_platform']
    df3.arrival = df3.arrival.apply(lambda x: sec2str(x))
    df3.to_csv(filename3)


def reconstruct_journey(destination, bag):
    jrny = []
    current = destination
    while current != 0:
        jrny.append((bag[current][2], bag[current][1], current))
        current = bag[current][2]
    jrny.reverse()
    return jrny


def print_journey(jrny, dep_time):
    """
    Print the given journey to logger info
    :param jrny: journey
    :param dep_time: Original requested departure
    :return: -
    """
    logger.info('Journey:')
    arr = dep_time
    if len(jrny) > 0:
        for leg in jrny:
            if leg[1] != 0:
                a = timetable.stops_array[leg[0]]
                b = timetable.stops_array[leg[2]]
                t = timetable.trips[timetable.trips.trip_id == leg[1]]
                tr = t.trip_short_name.values[0]
                dep = timetable.stop_times[(timetable.stop_times.index == leg[0]) &
                                           (timetable.stop_times.trip_id == leg[1])].departure_time.values
                arr = timetable.stop_times[(timetable.stop_times.index == leg[2]) &
                                           (timetable.stop_times.trip_id == leg[1])].arrival_time.values
                logger.info(str(sec2str(dep)) + " " + a[0].ljust(20) + '(p. ' + a[2].rjust(3) + ') TO ' +
                            str(sec2str(arr)) + " " + b[0].ljust(20) + '(p. ' + b[2].rjust(3) + ') WITH ' + str(tr))

        firstdepart = jrny[0] if jrny[0][1] != 0 else jrny[1]
        firstdepart = timetable.stop_times[(timetable.stop_times.index == firstdepart[0]) &
                                           (timetable.stop_times.trip_id == firstdepart[1])].departure_time.values[0]
        logger.info('Duration : {} ({} from request time {})'.format(sec2str(arr - firstdepart),
                                                                     sec2str(arr - str2sec(dep_time)),
                                                                     sec2str(str2sec(dep_time))))
    else:
        logger.info('No journey available')


def determine_disruptions(str_disruptions=""):
    """
    Determine all trip IDs that are disrupted.
    Input: ritnumbers (so-called shortnames) and tripseries (XX00)
    In case of a trip_serie all tripnumber XX01 through XX99 are added to the list
    :param str_disruptions:
    :return: True, if the list is populated
    """
    logger.debug(str_disruptions)
    if str_disruptions:
        str_ids = str_disruptions.split(" ")
        disruptions = []
        if (len(str_ids) > 0) & (len(str_disruptions) > 0):
            for str_id in str_ids:
                str_id = int(str_id)
                if str_id % 100:
                    disruptions.append(str_id)
                else:
                    for i in range(str_id + 1, str_id + 100):
                        disruptions.append(str_id)
        timetable.disruptions = timetable.trips[timetable.trips.trip_short_name.isin(disruptions)].trip_id.values
        return True
    else:
        timetable.disruptions = []
        return False


def parse_arguments():
    # --i gtfs-extracted --s "Arnhem Zuid" --e "Oosterbeek" --d "20210223" --t "08:30:00" --r 2 --c True
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input", type=str, default='gtfs-extracted', help="Input directory")
    parser.add_argument("-s", "--startpoint", type=str, default='Arnhem Zuid', help="Startpoint of the journey")
    parser.add_argument("-e", "--endpoint", type=str, default='Oosterbeek', help="Endpoint of the journey")
    parser.add_argument("-t", "--time", type=str, default='08:30', help="Departure time hh:mm:ss")
    parser.add_argument("-d", "--date", type=str, default='20210224', help="Departure date yyyymmdd")
    parser.add_argument("-r", "--rounds", type=int, default=8, help="Number of rounds to execute the RAPTOR algorithm")
    parser.add_argument("-c", "--cache", type=str2bool, default=True, help="Use cached GTFS")
    parser.add_argument("-f", "--full", type=str2bool, default=False, help="Use cached GTFS")
    parser.add_argument("-x", '--excluded', type=str, default="",
                        help="Space seperated list of disrupted tripnumbers/tripseries")
    arguments = parser.parse_args(sys.argv[1:])
    logger.debug('Parameters     : ' + str(sys.argv[1:]))
    logger.debug('Input directoy : ' + arguments.input)
    logger.debug('Start point    : ' + arguments.startpoint)
    logger.debug('End point      : ' + arguments.endpoint)
    logger.debug('Departure time : ' + arguments.time)
    logger.debug('Departure date : ' + arguments.date)
    logger.debug('Rounds         : ' + str(arguments.rounds))
    logger.debug('Cached GTFS    : ' + str(arguments.cache))
    return arguments


def optimize_timetable():
    """
    Optimize the timetable for usage in the raptor algorithm
    :return:
    """
    logger.debug('Optimizing GTFS data for algorithm')
    # stop ID's as integer
    timetable.stop_times.stop_id = timetable.stop_times.stop_id.astype(int)
    timetable.stops.stop_id = timetable.stops.stop_id.astype(int)
    # Remove unused columns from trips and stop_times
    timetable.trips.drop(['route_id', 'trip_headsign', 'trip_long_name', 'direction_id', 'shape_id'],
                         axis=1, inplace=True)
    timetable.trips = timetable.trips.merge(timetable.calendar[['service_id', 'date']], on='service_id')
    timetable.stop_times.drop(['shape_dist_traveled'], axis=1, inplace=True)
    timetable.stops.drop(['stop_lat', 'stop_lon', 'stop_code', 'zone_id'], axis=1, inplace=True)
    # Create dataset for mapping stop_ids to trips
    timetable.stop_times_for_trips = timetable.stop_times.copy()
    timetable.stop_times_for_trips = timetable.stop_times_for_trips.sort_index()
    # Clean stops data and add index for stop_id
    # Lookup table for parent_station to platforms
    timetable.station2stops = timetable.stops[['parent_station', 'stop_id']].set_index('parent_station')
    # Determine transfer stations (more than two direct destinations reachable)
    timetable.transfers = timetable.stop_times[['trip_id', 'stop_sequence', 'stop_id']].copy()
    timetable.transfers = timetable.transfers.sort_values(['trip_id', 'stop_sequence'])
    timetable.transfers['prev'] = timetable.transfers['trip_id'] == timetable.transfers['trip_id'].shift(-1)
    timetable.transfers['next_stop_id'] = timetable.transfers['stop_id'].shift(-1)
    timetable.transfers = timetable.transfers[timetable.transfers.prev &
                                              (timetable.transfers.stop_id != timetable.transfers.next_stop_id)]
    timetable.transfers = timetable.transfers[['stop_id', 'next_stop_id']]
    timetable.transfers = timetable.transfers.merge(timetable.stops[['stop_id', 'parent_station']])
    timetable.transfers.columns = ['stop_id', 'next_stop_id', 'parent_station']
    timetable.transfers = timetable.transfers[['parent_station', 'next_stop_id']].drop_duplicates()
    timetable.transfers = timetable.transfers.groupby('parent_station').count()
    timetable.transfers['transfer_station'] = timetable.transfers['next_stop_id'] > 2
    timetable.transfers.drop('next_stop_id', 1, inplace=True)
    # Move ID's to index
    timetable.stops.set_index('stop_id', inplace=True)
    timetable.stop_times.set_index('stop_id', inplace=True)
    timetable.stop_times_for_trips.set_index('trip_id', inplace=True)
    # Add transfer info to the stops info
    timetable.stops = timetable.stops.merge(timetable.transfers, left_on='parent_station', right_index=True)

    # Renumber stop_id's
    d = pd.DataFrame(timetable.stops.index.unique())
    d = d.sort_values('stop_id')
    d['new'] = range(0, len(d))
    d = d.set_index('stop_id').to_dict()['new']
    timetable.stops.index = timetable.stops.index.map(d)
    timetable.stop_times.index = timetable.stop_times.index.map(d)
    timetable.station2stops.stop_id = timetable.station2stops.stop_id.map(d)
    timetable.stop_times_for_trips.stop_id = timetable.stop_times_for_trips.stop_id.map(d)

    # Renumber trip_id's
    d = pd.DataFrame(timetable.trips.trip_id.unique(), columns=['trip_id'])
    d = d.sort_values('trip_id')
    d['new'] = range(0, len(d))
    d = d.set_index('trip_id').to_dict()['new']
    timetable.trips.trip_id = timetable.trips.trip_id.map(d)
    timetable.stop_times.trip_id = timetable.stop_times.trip_id.map(d)
    timetable.stop_times_for_trips.index = timetable.stop_times_for_trips.index.map(d)

    # Transform station ID's (stopareas) to numerical id's
    d = pd.DataFrame(timetable.stops.parent_station.unique(), columns=['station_id'])
    d = d.sort_values('station_id')
    d['new'] = range(0, len(d))
    d = d.set_index('station_id').to_dict()['new']
    timetable.station2stops.index = timetable.station2stops.index.map(d)
    timetable.stops.parent_station = timetable.stops.parent_station.map(d)

    # Add numpy array with stop info
    timetable.stops_array = timetable.stops.sort_index().to_numpy()

    # Add numy arrays with station 2 stops information
    dataset = timetable.station2stops.sort_index()
    cntidx = len(dataset.index.unique())
    cntdata = len(dataset)
    timetable.s2s_indexer = np.zeros(shape=(cntidx, 2), dtype=np.dtype(np.int32, np.int32))
    timetable.s2s_data = np.zeros(shape=cntdata, dtype=np.int32)
    timetable.s2s_data = dataset.stop_id.to_numpy()

    idxdata = dataset.groupby(dataset.index).count()
    idxdata['start'] = idxdata.stop_id.cumsum().shift(1).fillna(0).astype(int)
    timetable.s2s_indexer = idxdata[['start', 'stop_id']].to_numpy()


def store_optimized_data():
    """
    Store the timetable data to the cache directory
    :return:
    """
    logger.debug('Storing optimized timetable to cache directory')
    if not os.path.exists('optimized_timetable'):
        os.mkdir('optimized_timetable')
    timetable.agencies.to_pickle(os.path.join('optimized_timetable', 'agencies.pcl'))
    timetable.routes.to_pickle(os.path.join('optimized_timetable', 'routes.pcl'))
    timetable.trips.to_pickle(os.path.join('optimized_timetable', 'trips.pcl'))
    timetable.calendar.to_pickle(os.path.join('optimized_timetable', 'calendar.pcl'))
    timetable.stop_times.to_pickle(os.path.join('optimized_timetable', 'stop_times.pcl'))
    timetable.stops.to_pickle(os.path.join('optimized_timetable', 'stops.pcl'))
    timetable.station2stops.to_pickle(os.path.join('optimized_timetable', 'station2stops.pcl'))
    timetable.stop_times_for_trips.to_pickle(os.path.join('optimized_timetable', 'stop_times_for_trips.pcl'))
    timetable.transfers.to_pickle(os.path.join('optimized_timetable', 'transfers.pcl'))

    np.save(os.path.join('optimized_timetable', 'stops_array.npy'), timetable.stops_array, allow_pickle=True)
    np.save(os.path.join('optimized_timetable', 's2s_indexer.npy'), timetable.s2s_indexer, allow_pickle=True)
    np.save(os.path.join('optimized_timetable', 's2s_data.npy'), timetable.s2s_data, allow_pickle=True)
    np.save(os.path.join('optimized_timetable', 'disruptions.npy'), timetable.disruptions, allow_pickle=True)


def read_optimized_data():
    """
    Read the timetable data from the cache directory
    :return:
    """
    logger.debug('Using cached optimized datastructures')
    timetable.agencies = pd.read_pickle(os.path.join('optimized_timetable', 'agencies.pcl'))
    timetable.routes = pd.read_pickle(os.path.join('optimized_timetable', 'routes.pcl'))
    timetable.trips = pd.read_pickle(os.path.join('optimized_timetable', 'trips.pcl'))
    timetable.calendar = pd.read_pickle(os.path.join('optimized_timetable', 'calendar.pcl'))
    timetable.stop_times = pd.read_pickle(os.path.join('optimized_timetable', 'stop_times.pcl'))
    timetable.stops = pd.read_pickle(os.path.join('optimized_timetable', 'stops.pcl'))
    timetable.station2stops = pd.read_pickle(os.path.join('optimized_timetable', 'station2stops.pcl'))
    timetable.stop_times_for_trips = pd.read_pickle(os.path.join('optimized_timetable', 'stop_times_for_trips.pcl'))
    timetable.transfers = pd.read_pickle(os.path.join('optimized_timetable', 'transfers.pcl'))

    timetable.stops_array = np.load(os.path.join('optimized_timetable', 'stops_array.npy'), allow_pickle=True)
    timetable.s2s_indexer = np.load(os.path.join('optimized_timetable', 's2s_indexer.npy'), allow_pickle=True)
    timetable.s2s_data = np.load(os.path.join('optimized_timetable', 's2s_data.npy'), allow_pickle=True)
    timetable.disruptions = np.load(os.path.join('optimized_timetable', 'disruptions.npy'), allow_pickle=True)


def main(args):
    if args.cache & os.path.exists('optimized_timetable'):
        read_optimized_data()
        determine_disruptions(args.excluded)
    else:
        read_timetable(args.input)
        optimize_timetable()
        determine_disruptions(args.excluded)
        store_optimized_data()

    disrupt = len(timetable.disruptions) > 0

    if args.full:
        #
        # Perform a full network scan
        #
        ts = time.perf_counter()
        res_dict = {}
        for st in timetable.stops.stop_name.unique():
            logger.info('Calculating network from : {}'.format(st))
            traveltime, final_dest, stopbag = perform_lraptor(st, args.endpoint, args.date, args.time,
                                                              args.rounds, disrupt)
            res_dict[st] = (traveltime, stopbag)
        logger.info('lRaptor Algorithm executed in {:.4f} seconds'.format(time.perf_counter() - ts))

        with open(os.path.join('results', 'res_{date:%Y%m%d_%H%M%S}_dict.pcl'.format(date=datetime.now())), 'wb') as f:
            pickle.dump(res_dict, f, pickle.HIGHEST_PROTOCOL)
        logger.info('Finished full network scan')
    else:
        #
        # Find route between two stations
        #
        ts = time.perf_counter()
        traveltime, final_dest, stopbag = perform_lraptor(args.startpoint, args.endpoint,
                                                          args.date, args.time, args.rounds, disrupt)
        logger.info('lRaptor Algorithm executed in {:.4f} seconds'.format(time.perf_counter() - ts))

        if SAVE_RESULTS:
            export_results(traveltime, stopbag)

        journey = reconstruct_journey(final_dest, stopbag)
        print_journey(journey, args.time)


if __name__ == "__main__":
    # python -m cProfile -o out.prof lRaptor.py --i gtfs-extracted --s "Arnhem Zuid"
    #                                           --e "Oosterbeek" --d 20210223 --t "08:30:00" --r 2 --c True
    #
    # snakeviz out.prof
    #
    # main(Namespace(startpoint='Arnhem Zuid', endpoint='Oosterbeek', date='20210223' , time='08:30:00',
    #                cache=True, input='gtfs-extracted', rounds=2))
    #
    # main(Namespace(startpoint='Arnhem Zuid', endpoint='Oosterbeek', date='20210223' , time='08:30:00',
    #                cache=True, input='gtfs-extracted', rounds=2,  excluded='7625 6620'))
    argms = parse_arguments()

    main(argms)
