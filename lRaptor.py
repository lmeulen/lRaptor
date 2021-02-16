import os
import sys
import pandas as pd
import time
import logging
from datetime import datetime
import io
import argparse
from copy import copy
from dataclasses import dataclass

# Default transfer time is 3 minutes
TRANSFER_COST = (3 * 60)

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
    stops = None


def parse_time(time_str):
    """
    Convert hh:mm:ss to seconds since midnight
S
    """

    h, m, s = time_str.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def parse_sec_to_time(scnds):
    """
    Convert hh:mm:ss to seconds since midnight
    :param scnds:
    """

    h = int(scnds / 3600)
    m = int((scnds % 3600) / 60)
    s = int(scnds % 60)
    return "{:02d}:{:02d}:{:02d}".format(h, m, s)


def stop_time_to_str(stop):
    """
    Convert a GTFS stoptime location to a human readable string
    :param stop:
    :return:
    """

    s = str(stop['trip_id']) + '-' + str(stop['stop_sequence']) + ' ' + str(stop['stop_id']).ljust(2) + ' '
    s += str(stop['arrival_time']) + '-' + str(stop['departure_time']) + ' ' + str(stop['shape_dist_traveled'])
    return s


def stop_to_str(loc):
    """
    Convert a GTFS stop location location to a human readable string
    :param loc:
    :return:
    """

    s = str(loc['stop_id']) + ' - ' + str(loc['stop_name']) + ' - ' + str(loc['platform_code'])
    s += ' - ' + str(loc['stop_code']) + ' - ' + str(loc['parent_station'])
    return s


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


def get_trip_ids_for_stop(timetable, stop_id, departure_time, forward=60 * 60 * 24, filter=None):
    """Takes a stop and departure time and get associated trip ids.
       The forward parameter limits the time frame starting at the departure time.
       Default framesize is 60 minutes
       Times asre specified in seconds sincs midnight
    """

    mask_1 = timetable.stop_times.stop_id == stop_id
    mask_2 = timetable.stop_times.departure_time.between(departure_time, departure_time + forward)
    mask_3 = True
    if filter:
        mask_3 = ~timetable.stop_times.trip_id.isin(filter)
    # extract the list of qualifying trip ids
    potential_trips = timetable.stop_times[mask_1 & mask_2 & mask_3].trip_id.unique()
    return potential_trips.tolist()


def traverse_trips(timetable, current_ids, time_to_stops_orig, departure_time, filter_trips):
    """ Iterator through the stops reachable and add all new reachable stops
        by following all trips from the reached stations. Trips are only followed
        in the direction of travel and beyond already added points
    :param timetable: Timetable data
    :param current_ids: Current stops reached
    :param time_to_stops_orig: List of departure locations (e.g. multiple platforms for one station)
    :param departure_time: Departure time
    :param filter_trips: trips to filter from the list of potential trips
    """

    # prevent upstream mutation of dictionary
    extended_time_to_stops = copy(time_to_stops_orig)
    new_stops = []

    baseline_filter_trips = copy(filter_trips)
    logger.debug('        Filtered  trips: {}'.format(len(baseline_filter_trips)))
    filter_trips = []
    i = 0
    for ref_stop_id in current_ids:
        # how long it took to get to the stop so far (0 for start node)
        baseline_cost = extended_time_to_stops[ref_stop_id]
        # get list of all trips associated with this stop
        reachable_trips = get_trip_ids_for_stop(timetable, ref_stop_id, departure_time, filter=baseline_filter_trips)
        filter_trips.extend(reachable_trips)
        filter_trips = list(set(filter_trips))
        for potential_trip in reachable_trips:

            # get all the stop time arrivals for that trip
            stop_times_trip = timetable.stop_times[timetable.stop_times.trip_id == potential_trip]
            stop_times_trip = stop_times_trip.sort_values(by="stop_sequence")

            # get the "hop on" point
            from_here_subset = stop_times_trip[stop_times_trip.stop_id == ref_stop_id]
            from_here = from_here_subset.head(1).squeeze()

            # get all following stops
            stop_times_after = stop_times_trip[stop_times_trip.stop_sequence > from_here.stop_sequence]

            # for all following stops, calculate time to reach
            arrivals_zip = zip(stop_times_after.arrival_time, stop_times_after.stop_id)
            for arrive_time, arrive_stop_id in arrivals_zip:
                i += 1
                # time to reach is diff from start time to arrival (plus any baseline cost)
                arrive_time_adjusted = arrive_time - departure_time + baseline_cost

                # only update if does not exist yet or is faster
                if arrive_stop_id in extended_time_to_stops:
                    if extended_time_to_stops[arrive_stop_id] > arrive_time_adjusted:
                        extended_time_to_stops[arrive_stop_id] = arrive_time_adjusted
                else:
                    extended_time_to_stops[arrive_stop_id] = arrive_time_adjusted
                    new_stops.append(arrive_stop_id)
    logger.info('         Evaluations    : {}'.format(i))
    return extended_time_to_stops, new_stops, filter_trips


def add_transfer_time(timetable, current_ids, time_to_stops_orig, transfer_cost=TRANSFER_COST):
    """
    Add transfers between platforms
    :param timetable:
    :param current_ids:
    :param time_to_stops_orig:
    :param transfer_cost:
    :return:
    """

    # prevent upstream mutation of dictionary
    extended_time_to_stops = copy(time_to_stops_orig)
    new_stops = []

    # add in transfers to other platforms
    for stop_id in current_ids:
        stoparea = timetable.stops[timetable.stops.stop_id == stop_id]['parent_station'].values[0]

        # time to reach new nearby stops is the transfer cost plus arrival at last stop
        arrive_time_adjusted = extended_time_to_stops[stop_id] + transfer_cost

        # only update if currently inaccessible or faster than currrent option
        for arrive_stop_id in timetable.stops[timetable.stops.parent_station == stoparea]['stop_id']:
            if arrive_stop_id in extended_time_to_stops:
                if extended_time_to_stops[arrive_stop_id] > arrive_time_adjusted:
                    extended_time_to_stops[arrive_stop_id] = arrive_time_adjusted
            else:
                extended_time_to_stops[arrive_stop_id] = arrive_time_adjusted
                new_stops.append(arrive_stop_id)

    return extended_time_to_stops, new_stops


def determine_parameters(timetable, start_name, end_name, departure_time):
    """ Determine algorithm paramters based upon human readable information
        start_name : Location to start journey
        end_name: Endpoint of the journey
        departure_time: Time of the departure in hh:mm:ss
    """

    # look at all trips from that stop that are after the depart time
    departure = parse_time(departure_time)

    # get all information, including the stop ids, for the start and end nodes
    from_loc = timetable.stops[timetable.stops.stop_name == start_name]['stop_id'].to_list()
    to_loc = timetable.stops[timetable.stops.stop_name == end_name]['stop_id'].to_list()

    return from_loc, to_loc, departure


def final_destination(to_ids, reached_ids):
    """ Find the destination ID with the shortest distance
        Required in order to prevent adding travel time to the arrival time
    :param to_ids:
    :param reached_ids:
    :return:
    """

    final_id = ''
    distance = 999999
    for to_id in to_ids:
        if to_id in reached_ids:
            if reached_ids[to_id] < distance:
                distance = reached_ids[to_id]
                final_id = to_id
    return final_id


def read_timetable():
    """
    Read the timetable information from either cache or GTFS directory
    Global parameter READ_GTFS determines cache or reading original GTFS files
    :return:
    """

    start_time = time.perf_counter()
    tt = Timetable()
    if READ_GTFS:
        tt.agencies = pd.read_csv(os.path.join(GTFSDIR, 'agency.txt'))
        tt.routes = pd.read_csv(os.path.join(GTFSDIR, 'routes.txt'))
        tt.trips = pd.read_csv(os.path.join(GTFSDIR, 'trips.txt'))
        tt.trips.trip_short_name = tt.trips.trip_short_name.astype(int)
        tt.trips.shape_id = tt.trips.shape_id.astype('Int64')
        tt.calendar = pd.read_csv(os.path.join(GTFSDIR, 'calendar_dates.txt'))
        tt.calendar.date = tt.calendar.date.astype(str)
        tt.stop_times = pd.read_csv(os.path.join(GTFSDIR, 'stop_times.txt'))
        tt.stop_times.arrival_time = tt.stop_times.apply(lambda x: parse_time(x['arrival_time']), axis=1)
        tt.stop_times.departure_time = tt.stop_times.apply(lambda x: parse_time(x['departure_time']), axis=1)
        tt.stop_times.stop_id = tt.stop_times.stop_id.astype(str)
        tt.stops = pd.read_csv(os.path.join(GTFSDIR, 'stops.txt'))
        # Filter out the general station codes
        tt.stops = tt.stops[~tt.stops.stop_id.str.startswith('stoparea')]

        tt.agencies.to_pickle(os.path.join('timetable_cache', 'agencies.pcl'))
        tt.routes.to_pickle(os.path.join('timetable_cache', 'routes.pcl'))
        tt.trips.to_pickle(os.path.join('timetable_cache', 'trips.pcl'))
        tt.calendar.to_pickle(os.path.join('timetable_cache', 'calendar.pcl'))
        tt.stop_times.to_pickle(os.path.join('timetable_cache', 'stop_times.pcl'))
        tt.stops.to_pickle(os.path.join('timetable_cache', 'stops.pcl'))
    else:
        tt.agencies = pd.read_pickle(os.path.join('timetable_cache', 'agencies.pcl'))
        tt.routes = pd.read_pickle(os.path.join('timetable_cache', 'routes.pcl'))
        tt.trips = pd.read_pickle(os.path.join('timetable_cache', 'trips.pcl'))
        tt.calendar = pd.read_pickle(os.path.join('timetable_cache', 'calendar.pcl'))
        tt.stop_times = pd.read_pickle(os.path.join('timetable_cache', 'stop_times.pcl'))
        tt.stops = pd.read_pickle(os.path.join('timetable_cache', 'stops.pcl'))
    logger.info("Reading GTFS took {:0.4f} seconds".format(time.perf_counter() - start_time))
    logger.debug('Agencies  : {}'.format(len(tt.agencies)))
    logger.debug('Routes    : {}'.format(len(tt.routes)))
    logger.debug('Stops     : {}'.format(len(tt.stops)))
    logger.debug('Trips     : {}'.format(len(tt.trips)))
    logger.debug('Stoptimes : {}'.format(len(tt.stop_times)))
    return tt


def export_results(k, tt, filename):
    """
    Export results to a CSV file with stations and traveltimes (per iteration)
    :param k: datastructure with results per iteration
    :param tt: Timetable
    :param filename:  Filename
    :return: DataFrame with the results exported
    """

    logger.debug('Export results to {}'.format(filename))
    datastring = 'round,stop_id,stop_name,platform_code,travel_time\n'
    for i in list(k.keys()):
        locations = k[i]
        for destination in locations:
            traveltime = locations[destination]
            stop = tt.stops[tt.stops.stop_id == destination]
            name = stop['stop_name'].values[0]
            platform = stop['platform_code'].values[0]
            datastring += (str(i) + ',' + str(destination) + ',' + str(name) + ',' + str(platform) + ',' +
                           str(traveltime) + '\n')
    df = pd.read_csv(io.StringIO(datastring), sep=",")
    df.travel_time = df.travel_time
    df = df[['round', 'stop_name', 'travel_time']].groupby(['round', 'stop_name']).min().sort_values('travel_time')
    df.travel_time = df.apply(lambda x: parse_sec_to_time(x.travel_time), axis=1)
    df.to_csv(filename)
    return df


def perform_lraptor(time_table, departure_name, arrival_name, departure_time, iterations):
    """
    Perform the Raptor algorithm
    :param time_table: Time table
    :param departure_name: Name of departure location
    :param arrival_name: Name of arrival location
    :param departure_time: Time of departure, str format (hh:mm:sss)
    :param iterations: Number of iterations to perform
    :return:
    """

    # Determine start and stop area
    (from_stops, to_stops, dep_secs) = determine_parameters(time_table, departure_name, arrival_name, departure_time)
    logger.debug('Departure ID : ' + str(from_stops))
    logger.debug('Arrival ID   : ' + str(to_stops))
    logger.debug('Time         : ' + str(dep_secs))

    # initialize lookup with start node taking 0 seconds to reach
    k_results = {}
    reached_stops = {}
    new_stops_total = []
    filter_trips = []
    for from_stop in from_stops:
        reached_stops[from_stop] = 0
        new_stops_total.append(from_stop)
    logger.debug('Starting from IDS : '.format(str(reached_stops)))

    for k in range(1, iterations + 1):
        logger.info("Analyzing possibilities round {}".format(k))

        # get list of stops to evaluate in the process
        stops_to_evaluate = list(new_stops_total)
        logger.info("    Stops to evaluate count: {}".format(len(stops_to_evaluate)))

        # update time to stops calculated based on stops accessible
        t = time.perf_counter()
        reached_stops, new_stops_travel, filter_trips = \
            traverse_trips(time_table, stops_to_evaluate, reached_stops, dep_secs, filter_trips)
        logger.info("    Travel stops  calculated in {:0.4f} seconds".format(time.perf_counter() - t))
        logger.info("    {} stops added".format(len(new_stops_travel)))

        # now add footpath transfers and update
        t = time.perf_counter()
        stops_to_evaluate = list(reached_stops.keys())
        reached_stops, new_stops_transfer = add_transfer_time(time_table, stops_to_evaluate, reached_stops)
        logger.info("    Transfers calculated in {:0.4f} seconds".format(time.perf_counter() - t))
        logger.info("    {} stops added".format(len(new_stops_transfer)))

        new_stops_total = set(new_stops_travel).union(new_stops_transfer)

        logger.info("    {} stops to evaluate in next round".format(len(new_stops_total)))

        # Store the results for this round
        k_results[k] = reached_stops

    # Determine the best destionation ID, destination is a platform.
    dest_id = final_destination(to_stops, reached_stops)
    if dest_id != '':
        logger.info("Destination code   : {} ".format(dest_id))
        logger.info("Time to destination: {} minutes".format(reached_stops[dest_id] / 60))
    else:
        logger.info("Destination unreachable with given parameters")
    return k_results


if __name__ == "__main__":

    # --i gtfs-extracted --s "Arnhem Zuid" --e "Oosterbeek" --d "08:30:00" --r 1 --c True
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input", type=str, help="Input directory")
    parser.add_argument("-s", "--startpoint", type=str, help="Startpoint of the journey")
    parser.add_argument("-e", "--endpoint", type=str, help="Endpoint of the journey")
    parser.add_argument("-d", "--departure", type=str, help="Departure time hh:mm:ss")
    parser.add_argument("-r", "--rounds", type=int, help="Number of rounds to execute the RAPTOR algorithm")
    parser.add_argument("-c", "--cache", type=str2bool, default=False, help="Use cached GTFS")
    args = parser.parse_args(sys.argv[1:])

    logger.debug('Input directoy : ' + args.input)
    logger.debug('Start point    : ' + args.startpoint)
    logger.debug('End point      : ' + args.endpoint)
    logger.debug('Departure time : ' + args.departure)
    logger.debug('Rounds         : ' + str(args.rounds))
    logger.debug('Cached GTFS    : ' + str(args.cache))

    GTFSDIR = args.input
    READ_GTFS = not args.cache
    DEPARTURE = args.startpoint
    ARRIVAL = args.endpoint
    DEP_TIME = args.departure
    ROUNDS = args.rounds

    time_table_NS = read_timetable()

    result = perform_lraptor(time_table_NS, DEPARTURE, ARRIVAL, DEP_TIME, ROUNDS)
    res = export_results(result, time_table_NS, 'results_{date:%Y%m%d_%H%M%S}.csv'.format(date=datetime.now()))
