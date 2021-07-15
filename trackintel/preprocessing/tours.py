from datetime import timedelta
import geopandas as gpd
import pandas as pd
import numpy as np
from tqdm import tqdm
from pyproj import Geod

import trackintel as ti


def generate_tours(
    trips_inp,
    max_dist,
    geom_col="geom",
    print_progress=False,
    min_tour_length=2,
    max_gap_size=0,
    max_time=timedelta(days=1),
):
    """
    Generate trackintel-tours from trips
    - Tours are defined as a collection of trips in a certain time frame that start and end at the same point
    - Nested tours are possible and will be regarded as two distinct tours

    Parameters
    ----------
    trips_inp : GeoDataFrame

    Returns
    -------
    tour_dict_entry: dictionary

    """
    assert geom_col in trips_inp.columns, "Trips table must be a GeoDataFrame and requires a geomentry column"

    if print_progress:
        tqdm.pandas(desc="User trip generation")
        tours = (
            trips_inp.groupby(["user_id"], group_keys=False, as_index=False)
            .progress_apply(
                _generate_tours_user,
                max_dist=max_dist,
                min_tour_length=min_tour_length,
                max_gap_size=max_gap_size,
                max_time=max_time,
            )
            .reset_index(drop=True)
        )
    else:
        tours = (
            trips_inp.groupby(["user_id"], group_keys=False, as_index=False)
            .apply(
                _generate_tours_user,
                max_dist=max_dist,
                min_tour_length=min_tour_length,
                max_gap_size=max_gap_size,
                max_time=max_time,
            )
            .reset_index(drop=True)
        )

    # # index management TODO
    tours["id"] = np.arange(len(tours))
    tours.set_index("id", inplace=True)

    # # assign trip_id to tpls
    tour2trip_map = tours[["trips"]].to_dict()["trips"]
    ls = []
    for key, values in tour2trip_map.items():
        for value in values:
            ls.append([value, key])
    temp = pd.DataFrame(ls, columns=[trips_inp.index.name, "tour_id"]).set_index(trips_inp.index.name)
    trips_inp = trips_inp.join(temp, how="left")

    # TODO: assign tour id to trips

    # TODO cleanup

    ## dtype consistency
    # trips id (generated by this function) should be int64
    tours.index = tours.index.astype("int64")
    # trips_inp["tour_id"] = trips_inp["tour_id"].astype("Int64")

    return trips_inp, tours


def _generate_tours_user(user_trip_df, max_dist=100, min_tour_length=2, max_gap_size=0, max_time=timedelta(days=1)):
    assert min_tour_length >= 2, "Tour must consist of at least 2 trips!"
    user_id = user_trip_df["user_id"].unique()
    assert len(user_id) == 1
    user_id = user_id[0]

    # sort by time
    user_trip_df = user_trip_df.sort_values(by=["started_at"])

    # save only the trip id (row.name) in the start candidates
    start_candidates = []

    # collect tours
    tours = []
    # Iterate over trips
    for i, row in user_trip_df.iterrows():
        trip_id = row.name  # trip id
        end_time = row["finished_at"]
        start_point = row["geom"][0]
        end_point = row["geom"][1]
        # print()
        # print("current point:", trip_id, start_point, end_point)
        # print("current candidates", start_candidates)

        # check if there is a gap between last and this trip
        if len(start_candidates) > 0:
            # compare end of last to start of new
            dist_to_last = _get_point_dist(user_trip_df.loc[start_candidates[-1], "geom"][1], start_point)
            if dist_to_last > max_dist:
                # option 1: no gaps allowed - start search again
                if max_gap_size == 0:
                    start_candidates = [row.name]
                    continue
                # option 2: gaps allowed - search further
                else:
                    start_candidates.append(np.nan)

        # Add this point as a candidate
        start_candidates.append(row.name)

        # Check if tour would be long enough
        if len(start_candidates) < min_tour_length:
            continue

        # Check whether endpoint would be an unkown activity
        if pd.isna(row["destination_staypoint_id"]):
            continue

        # keep a list of which candidates to remove (because of time frame)
        new_list_start = 0

        # check distance to all candidates (except the ones that are too close)
        for j, cand in enumerate(start_candidates[: -min_tour_length + 1]):
            #             print("------", j, cand)
            # gap
            if np.isnan(cand):
                continue

            # check time difference - if time too long, we can remove the candidate
            cand_start_time = user_trip_df.loc[cand, "started_at"]
            # print("time diff", end_time - cand_start_time)
            if end_time - cand_start_time > max_time:
                new_list_start = j + 1
                # print("removed candidate because time too long", j + 1)
                continue

            # check whether the start-end candidate of a tour is an unkown activity
            if pd.isna(user_trip_df.loc[cand, "origin_staypoint_id"]):
                continue

            cand_start_point = user_trip_df.loc[cand, "geom"][0]

            # TODO: compute length of triplegs and sum - must be larger than minthresh

            # check if endpoint of trip = start location of cand
            point_dist = _get_point_dist(end_point, cand_start_point)
            # print("Check distance to start", cand, end_point, cand_start_point, point_dist)
            if point_dist < max_dist:
                # Tour found!
                # collect the trips on the tour in a list
                non_gap_trip_idxs = [c for c in start_candidates[j:] if ~np.isnan(c)]
                tour_candidate = user_trip_df[user_trip_df.index.isin(non_gap_trip_idxs)]
                tours.append(_create_tour_from_stack(tour_candidate, max_dist, max_time))

                nr_gaps = np.sum(np.isnan(np.array(start_candidates[j:])))

                if nr_gaps > max_gap_size:
                    # No tour found, too many gaps inbetween
                    continue
                # print("Tour found!", tour_candidate.head())
                # _visualize_tour(tour_candidate)  # TODO

                # remove trips that were on the tour
                start_candidates = start_candidates[:j]
                # remove gap if there is a gap in the end
                if np.isnan(start_candidates[-1]):
                    del start_candidates[-1]
                # do not consider the other trips - one trip cannot close two tours at a time anyway
                break

        # remove points because they are out of the time window
        start_candidates = start_candidates[new_list_start:]
        # print("afterwards: ", start_candidates)

    tours_df = pd.DataFrame(tours)
    return tours_df


def _visualize_tour(tour_table):
    import matplotlib.pyplot as plt

    plot_tour = []
    for i, row in tour_table.iterrows():
        plot_tour.append([row["geom"][0].x, row["geom"][0].y])
        plot_tour.append([row["geom"][1].x, row["geom"][1].y])
    plot_tour = np.array(plot_tour)
    print(plot_tour)
    plt.figure(figsize=(8, 2))
    plt.plot(plot_tour[:, 0], plot_tour[:, 1])
    plt.show()


def _get_point_dist(p1, p2):
    """
    p1, p2: gdp Point objects
    Returns: Distance of points
    """
    geod = Geod(ellps="CPM")  # other guy used WGS84
    dist = geod.inv(p1.x, p1.y, p2.x, p2.y)[2]
    return dist


def _create_tour_from_stack(temp_tour_stack, max_dist, max_time):
    """
    Aggregate information of tour elements in a structured dictionary.

    Parameters
    ----------
    temp_tour_stack : list
        list of dictionary like elements (either pandas series or python dictionary).
        Contains all trips that will be aggregated into a tour

    Returns
    -------
    tour_dict_entry: dictionary

    """
    # this function return and empty dict if no tripleg is in the stack
    first_trip = temp_tour_stack.iloc[0]
    last_trip = temp_tour_stack.iloc[-1]

    # all data has to be from the same user
    assert len(temp_tour_stack["user_id"].unique()) == 1

    # double check if tour requirements are fulfilled
    assert last_trip["finished_at"] - first_trip["started_at"] < max_time
    assert _get_point_dist(last_trip["geom"][1], first_trip["geom"][0]) < max_dist

    # TODO: get unique staypoints along the tour

    tour_dict_entry = {
        "user_id": first_trip["user_id"],
        "started_at": first_trip["started_at"],
        "finished_at": last_trip["finished_at"],
        "origin_staypoint_id": first_trip.name,
        "destination_staypoint_id": last_trip.name,
        "trips": list(temp_tour_stack.index),
    }

    return tour_dict_entry


if __name__ == "__main__":
    import os

    # create trips from geolife (based on positionfixes)
    # pfs, _ = ti.io.dataset_reader.read_geolife(os.path.join("tests", "data", "geolife_long"))
    # pfs, stps = pfs.as_positionfixes.generate_staypoints(
    #     method="sliding", dist_threshold=25, time_threshold=5, gap_threshold=1e6
    # )
    # stps = stps.as_staypoints.create_activity_flag(time_threshold=15)
    # pfs, tpls = pfs.as_positionfixes.generate_triplegs(stps)

    # # generate trips and a joint staypoint/triplegs dataframe
    # stps, tpls, trips = ti.preprocessing.triplegs.generate_trips(stps, tpls, gap_threshold=15)
    trips = ti.io.file.read_trips_csv(os.path.join("tests", "data", "geolife_long", "trips.csv"), index_col="id")

    # trips_user = trips[trips["user_id"] == 1]
    tours = generate_tours(trips, 30)
