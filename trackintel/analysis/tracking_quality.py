import datetime

import pandas as pd
import numpy as np
import warnings


def temporal_tracking_quality(source, granularity="all", max_iter=60):
    """
    Calculate per-user temporal tracking quality (temporal coverage).

    Parameters
    ----------
    df : GeoDataFrame (as trackintel datamodels)
        The source dataframe to calculate temporal tracking quality.

    granularity : {"all", "day", "week", "weekday", "hour"}
        The level of which the tracking quality is calculated. The default "all" returns
        the overall tracking quality; "day" the tracking quality by days; "week" the quality
        by weeks; "weekday" the quality by day of the week (e.g, Mondays, Tuesdays, etc.) and
        "hour" the quality by hours.

    max_iter: int, default 60
        The maximum iteration when spliting the df per granularity to calculate tracking quality.
        The unit is the same as the "granularity", e.g., if "max_iter" = 60 and "granularity" = "day",
        df that contains records with a duration more than 60 days cannot be processed and the quality
        result is likely to be wrong. if "max_iter" is reached, consider pre-filter the df instead of
        changing the "max_iter" parameter.

    Returns
    -------
    quality: DataFrame
        A per-user per-granularity temporal tracking quality dataframe.

    Notes
    -----
    Requires at least the following columns:
    ``['user_id', 'started_at', 'finished_at']``
    which means the function supports trackintel ``staypoints``, ``triplegs``, ``trips`` and ``tours``
    datamodels and their combinations (e.g., staypoints and triplegs sequence).

    The temporal tracking quality is the ratio of tracking time and the total time extent. It is
    calculated and returned per-user in the defined ``granularity``. The possible time extents of
    the different granularities are different:

    - ``all`` considers the time between the latest "finished_at" and the earliest "started_at";
    - ``week`` considers the whole week (604800 sec)
    - ``day`` and ``weekday`` consider the whole day (86400 sec)
    - ``hour`` considers the whole hour (3600 sec).

    The tracking quality of each user is calculated based on his or her own tracking extent.
    For granularity = ``day`` or ``week``, the quality["day"] or quality["week"] column displays the
    time relative to the first record in the entire dataset.

    In addition to the relative values of the chosen granularity w.r.t the first recrod, when the granularity
    is either "day" or "week", the quality dataframe has an extra column called "date" or "week_number" respectively.
    Using the "date" and "week_number" columns, the trackintel user can estimate which specific
    "date" or "week_number" had poor tracking quality


    Examples
    --------
    >>> # calculate overall tracking quality of stps
    >>> temporal_tracking_quality(stps, granularity="all")
    >>> # calculate per-day tracking quality of stps and tpls sequence
    >>> temporal_tracking_quality(stps_tpls, granularity="day")
    """
    required_columns = ["user_id", "started_at", "finished_at"]
    if any([c not in source.columns for c in required_columns]):
        raise KeyError(
            "To successfully calculate the user-level tracking quality, "
            + "the source dataframe must have the columns [%s], but it has [%s]."
            % (", ".join(required_columns), ", ".join(source.columns))
        )

    df = source.copy()
    df.reset_index(inplace=True)

    # filter out records with duration <= 0
    df["duration"] = (df["finished_at"] - df["started_at"]).dt.total_seconds()
    df = df.loc[df["duration"] > 0].copy()
    # ensure proper handle of empty dataframes
    if len(df) == 0:
        warnings.warn(f"The input dataframe does not contain any record with positive duration. Please check.")
        return None

    if granularity == "all":
        quality = df.groupby("user_id", as_index=False).apply(_get_tracking_quality_user, granularity)

    elif granularity == "day":
        # split records that span several days
        df = _split_overlaps(df, granularity=granularity, max_iter=max_iter)
        # get the tracked day relative to the first day
        start_date = df["started_at"].min().date()
        df["day"] = df["started_at"].apply(lambda x: (x.date() - start_date).days)
        dict_start_day_to_date = dict(zip(df["day"], df["started_at"].apply(lambda x: x.date())))
        dict_finish_day_to_date = dict(zip(df["day"], df["finished_at"].apply(lambda x: x.date())))
        dict_day_to_date = {**dict_start_day_to_date, **dict_finish_day_to_date}

        # calculate per-user per-day raw tracking quality
        raw_quality = df.groupby(["user_id", "day"], as_index=False).apply(_get_tracking_quality_user, granularity)
        # add quality = 0 records
        quality = _get_all_quality(df, raw_quality, granularity)
        quality["date"] = quality["day"].map(dict_day_to_date)

    elif granularity == "week":
        # split records that span several days
        df = _split_overlaps(df, granularity="day", max_iter=max_iter)
        # get the tracked week relative to the first day
        start_date = df["started_at"].min().date()
        df["week"] = df["started_at"].apply(lambda x: (x.date() - start_date).days // 7)
        dict_start_week_num = dict(
            zip(df["week"], df["started_at"].apply(lambda x: pd.to_datetime(x).isocalendar()[1]))
        )
        dict_finish_week_num = dict(
            zip(df["week"], df["finished_at"].apply(lambda x: pd.to_datetime(x).isocalendar()[1]))
        )
        dict_week_num = {**dict_start_week_num, **dict_finish_week_num}

        # calculate per-user per-week raw tracking quality
        raw_quality = df.groupby(["user_id", "week"], as_index=False).apply(_get_tracking_quality_user, granularity)
        # add quality = 0 records
        quality = _get_all_quality(df, raw_quality, granularity)
        quality["week_number"] = quality["week"].map(dict_week_num).astype("int32")

    elif granularity == "weekday":
        # split records that span several days
        df = _split_overlaps(df, granularity="day", max_iter=max_iter)

        # get the tracked week relative to the first day
        start_date = df["started_at"].min().date()
        df["week"] = df["started_at"].apply(lambda x: (x.date() - start_date).days // 7)
        # get the weekday
        df["weekday"] = df["started_at"].dt.weekday

        # calculate per-user per-weekday raw tracking quality
        raw_quality = df.groupby(["user_id", "weekday"], as_index=False).apply(_get_tracking_quality_user, granularity)
        # add quality = 0 records
        quality = _get_all_quality(df, raw_quality, granularity)

    elif granularity == "hour":
        # first do a day split to speed up the hour split
        df = _split_overlaps(df, granularity="day", max_iter=max_iter)
        df = _split_overlaps(df, granularity=granularity)

        # get the tracked day relative to the first day
        start_date = df["started_at"].min().date()
        df["day"] = df["started_at"].apply(lambda x: (x.date() - start_date).days)
        # get the hour
        df["hour"] = df["started_at"].dt.hour

        # calculate per-user per-hour raw tracking quality
        raw_quality = df.groupby(["user_id", "hour"], as_index=False).apply(_get_tracking_quality_user, granularity)
        # add quality = 0 records
        quality = _get_all_quality(df, raw_quality, granularity)

    else:
        raise AttributeError(
            f"granularity unknown. We only support ['all', 'day', 'week', 'weekday', 'hour']. You passed {granularity}"
        )

    return quality


def _get_all_quality(df, raw_quality, granularity):
    """
    Add tracking quality values for empty bins.

    raw_quality is calculated using `groupby` and does not report bins (=granularties) with
    quality = 0. This function adds these values.

    Parameters
    ----------
    df : GeoDataFrame (as trackintel datamodels)

    raw_quality: DataFrame
        The calculated raw tracking quality directly from the groupby operations.

    granularity : {"all", "day", "weekday", "week", "hour"}
        Used for accessing the column in raw_quality.

    Returns
    -------
    quality: pandas.Series
        A pandas.Series object containing the tracking quality
    """
    all_users = df["user_id"].unique()
    all_granularity = np.arange(df[granularity].max() + 1)
    # construct array containing all user and granularity combinations
    all_combi = np.array(np.meshgrid(all_users, all_granularity)).T.reshape(-1, 2)
    # the records with no corresponding raw_quality is nan, and transformed into 0
    all_combi = pd.DataFrame(all_combi, columns=["user_id", granularity])
    quality = all_combi.merge(raw_quality, how="left", on=["user_id", granularity], validate="one_to_one")
    quality.fillna(0, inplace=True)
    return quality


def _get_tracking_quality_user(df, granularity="all"):
    """
    Tracking quality per-user per-granularity.

    Parameters
    ----------
    df : GeoDataFrame (as trackintel datamodels)
        The source dataframe

    granularity : {"all", "day", "weekday", "week", "hour"}, default "all"
        Determines the extent of the tracking. "all" the entire tracking period,
        "day" and "weekday" a whole day, "week" a whole week, and "hour" a whole hour.

    Returns
    -------
    pandas.Series
        A pandas.Series object containing the tracking quality
    """
    tracked_duration = (df["finished_at"] - df["started_at"]).dt.total_seconds().sum()
    if granularity == "all":
        # the whole tracking period
        extent = (df["finished_at"].max() - df["started_at"].min()).total_seconds()
    elif granularity == "day":
        # total seconds in a day
        extent = 60 * 60 * 24
    elif granularity == "weekday":
        # total seconds in an day * number of tracked weeks
        # (entries from multiple weeks may be grouped together)
        extent = 60 * 60 * 24 * (df["week"].max() - df["week"].min() + 1)
    elif granularity == "week":
        # total seconds in a week
        extent = 60 * 60 * 24 * 7
    elif granularity == "hour":
        # total seconds in an hour * number of tracked days
        # (entries from multiple days may be grouped together)
        extent = (60 * 60) * (df["day"].max() - df["day"].min() + 1)
    else:
        raise AttributeError(
            f"granularity unknown. We only support ['all', 'day', 'week', 'weekday', 'hour']. You passed {granularity}"
        )
    return pd.Series([tracked_duration / extent], index=["quality"])


def _split_overlaps(source, granularity="day", max_iter=60):
    """
    Split input df that have a duration of several days or hours.

    Parameters
    ----------
    source : GeoDataFrame (as trackintel datamodels)
        The source to perform the split

    granularity : {'day', 'hour'}, default 'day'
        The criteria of splitting. "day" splits records that have duration of several
        days and "hour" splits records that have duration of several hours.

    max_iter: int, default 60
        The maximum iteration when spliting the source per granularity.
        See :func:`trackintel.analysis.tracking_quality.temporal_tracking_quality` for a more detailed explaination.

    Returns
    -------
    GeoDataFrame (as trackintel datamodels)
        The GeoDataFrame object after the splitting
    """
    df = source.copy()
    change_flag = __get_split_index(df, granularity=granularity)

    iter_count = 0

    # Iteratively split one day/hour from multi day/hour entries until no entry spans over multiple days/hours
    while change_flag.sum() > 0:

        # calculate new finished_at timestamp (00:00 midnight)
        finished_at_temp = df.loc[change_flag, "finished_at"].copy()
        if granularity == "day":
            df.loc[change_flag, "finished_at"] = df.loc[change_flag, "started_at"].apply(
                lambda x: x.replace(hour=23, minute=59, second=59) + datetime.timedelta(seconds=1)
            )
        elif granularity == "hour":
            df.loc[change_flag, "finished_at"] = df.loc[change_flag, "started_at"].apply(
                lambda x: x.replace(minute=59, second=59) + datetime.timedelta(seconds=1)
            )

        # create new entries with remaining timestamp
        new_df = df.loc[change_flag].copy()
        new_df.loc[change_flag, "started_at"] = df.loc[change_flag, "finished_at"]
        new_df.loc[change_flag, "finished_at"] = finished_at_temp

        df = df.append(new_df, ignore_index=True, sort=True)

        change_flag = __get_split_index(df, granularity=granularity)
        iter_count += 1

        if iter_count > max_iter:
            warnings.warn(
                f"Maximum iteration {max_iter} reached when splitting the input dataframe by {granularity}. "
                "Consider checking the timeframe of the input or parsing a higher 'max_iter' parameter."
            )
            break

    if "duration" in df.columns:
        df["duration"] = df["finished_at"] - df["started_at"]

    return df


def __get_split_index(df, granularity="day"):
    """
    Get the index that needs to be splitted.

    Parameters
    ----------
    df : GeoDataFrame (as trackintel datamodels)
        The source to perform the split.

    granularity : {'day', 'hour'}, default 'day'
        The criteria of spliting. "day" splits records that have duration of several
        days and "hour" splits records that have duration of several hours.

    Returns
    -------
    change_flag: pd.Series
        Boolean index indicating which records needs to be splitted
    """
    change_flag = df["started_at"].dt.date != (df["finished_at"] - pd.to_timedelta("1s")).dt.date
    if granularity == "hour":
        hour_flag = df["started_at"].dt.hour != (df["finished_at"] - pd.to_timedelta("1s")).dt.hour
        # union of day and hour change flag
        change_flag = change_flag | hour_flag

    return change_flag
