import inspect
import warnings

import xarray as xr

from cmip6_preprocessing.utils import _key_from_attrs, cmip6_dataset_id


# define the attrs that are needed to get an 'exact' match
exact_attrs = [
    "source_id",
    "grid_label",
    "experiment_id",
    "table_id",
    # "version", # for testing
    # "member_id",
    "variant_label",
]


def _match_attrs(ds_a, ds_b, match_attrs):
    """returns the number of matched attrs between two datasets"""
    return sum([ds_a.attrs[i] == ds_b.attrs[i] for i in match_attrs])


def match_datasets(ds, ds_dict, match_attrs, pop=True):
    """Find all datasets in a dictionary of datasets that match a set of attributes.

    and return a list of datasets for merging/concatting.
    Optionally remove the matching datasets from the input dict.

    Parameters
    ----------
    ds : xr.Dataset
        Input dataset. This is the reference to which matches will be searched.
    ds_dict : dict
        Dictionary of xr.Datasets, containing `candidates` for matching.
    match_attrs : list
        List of strings, correponding to the dataset attributes to match
    pop : bool, optional
        If true, removes matched datasets from `ds_dict`, by default True

    Returns
    -------
    list
        List of xr.Datasets. Each list will contain the input `ds` as first entry
        and after that possible matching results. Each matched dataset
        receives an additional attrs, containing the original key of the `ds_dict`.
    """
    datasets = [ds]
    keys = list(ds_dict.keys())
    for k in keys:
        # if match is 'exact' (according to the match_attrs), pop the dataset from the dict
        if _match_attrs(ds, ds_dict[k], match_attrs) == len(match_attrs):
            if pop:
                ds_matched = ds_dict.pop(k)
            else:
                ds_matched = ds_dict[k]
            ds_matched.attrs["original_key"] = k
            datasets.append(ds_matched)
    return datasets


def parse_metric(ds, metric, dim_length_conflict="error"):
    """Convenience function to parse a metric dataarry into an existing dataset.

    Parameters
    ----------
    ds : xr.Dataset
        Input dataset
    metric : xr.Data
        Metric dataarray to add to `ds`
    dim_length_conflict : str
        Specifies how to handle dimensions that are not the same lenght.

    Returns
    -------
    xr.Dataset
        `ds` with `metric` added as coordinate.

    """
    dataset_id = cmip6_dataset_id(ds)

    # TODO: Show the 'id' in the failures and warnings.
    if not isinstance(metric, xr.DataArray):
        raise ValueError(
            f"{dataset_id}:`metric` input must be xarray.DataArray. Got {type(metric)}"
        )

    if metric.name is None:
        warnings.warn(
            f"{dataset_id}:`metric` has no name. This might lead to problems down the line.",
            RuntimeWarning,
        )

    # Check dimension compatibility
    # TODO: Check if xarray has some nifty way to do this
    mismatch_dims = []
    for di in metric.dims:
        if len(ds[di]) != len(metric[di]):
            mismatch_dims.append(di)

    if len(mismatch_dims) > 0:
        metric_str = [str(di) + ":" + str(len(metric[di])) for di in mismatch_dims]
        ds_str = [str(di) + ":" + str(len(ds[di])) for di in mismatch_dims]
        msg = (
            f"{dataset_id}:`metric` dimensions {metric_str} do not match `ds` {ds_str}."
        )
        if dim_length_conflict == "error":
            raise ValueError(msg)
        elif dim_length_conflict == "align":
            warnings.warn(msg + " Aligning the data on `inner`")
            ds, metric = xr.align(ds, metric, join="inner")

    # strip all coordinates from metric
    metric_stripped = metric.reset_coords(drop=True)

    # add attributes
    metric_stripped.attrs[
        "parsed_with"
    ] = f"cmip6_preprocessing/postprocessing/{inspect.currentframe().f_code.co_name}"
    # TODO: Get the package and module name without hardcoding. But not as important I think.

    ds = ds.assign_coords({metric_stripped.name: metric_stripped})

    return ds


def match_metrics(
    ds_dict,
    metric_dict,
    match_variables,
    match_attrs=["source_id", "grid_label"],
    print_statistics=False,
    exact_attrs=exact_attrs,
    dim_length_conflict="error",
):
    """Given two dictionaries of datasets, this function matches metrics from `metric_dict` to
    every datasets in `ds_dict` based on comparing the datasets attributes.

    Parameters
    ----------
    ds_dict : dict
        Dictionary of xarray datasets, that need matching metrics.
    metric_dict : dict
        Dictionary of xarray datasets, which contain metrics as data_variables.
    match_variables : list
        Data variables of datasets in `metric_dict` to match.
    match_attrs : list, optional
        Minimum dataset attributes that need to match, by default ["source_id", "grid_label"]
    print_statistics : bool, optional
        Option to print statistics about matching, by default False
    exact_attrs : list
        List of attributes that define an `exact` match, by
        default ["source_id","grid_label","experiment_id","table_id", "variant_label"].
    dim_length_conflict : str
        Defines the behavior when parsing metrics with non-exact matches in dimension size.
        See `parse_metric`.
    Returns
    -------
    dict
        All datasets from `ds_dict`, if match was not possible the input dataset is returned.

    """
    # Very rudimentary check if the data was aggregated with intake-esm
    # (which leaves non-correct attributes behind see: https://github.com/intake/intake-esm/issues/264)
    if any(["member_id" in ds.dims for ds in ds_dict.values()]):
        raise ValueError(
            "It seems like some of the input datasets in `ds_dict` have aggregated members.\
            This is not currently supported. Please use `aggregate=False` when using intake-esm `to_dataset_dict()`."
        )

    # if match is set to exact check all these attributes
    if match_attrs == "exact":
        match_attrs = exact_attrs

    total_datasets = 0
    exact_datasets = {va: 0 for va in match_variables}
    nonexact_datasets = {va: 0 for va in match_variables}
    nomatch_datasets = {va: 0 for va in match_variables}

    ds_dict_parsed = {}
    for k, ds in ds_dict.items():
        total_datasets += 1
        # Filter for the matching attrs
        matched_metrics = match_datasets(ds, metric_dict, match_attrs, pop=False)

        # Filter for the variable
        for mv in match_variables:
            matched_metric_vars = [ds for ds in matched_metrics if mv in ds.variables]

            if len(matched_metric_vars) == 0:
                warnings.warn(f"No matching metrics found for {mv}")
                nomatch_datasets[mv] += 1
            else:
                # Pick the metric with the most mathching attributes.
                # This ensures that the exact match is picked if available.

                nmatch = [
                    _match_attrs(ds, ds_match, exact_attrs)
                    for ds_match in matched_metric_vars
                ]
                sorted_by_match = [
                    x for _, x in sorted(zip(nmatch, matched_metric_vars))
                ]

                ds_metric = sorted_by_match[-1]

                # this is a hardcoded check for time variable metrics.
                # These are very likely only valid for exact matches of runs.
                # For instance these could be cell thickness values, which cannot simply be used
                # for all runs of a model.
                exact_match = _match_attrs(ds, ds_metric, exact_attrs) == len(
                    exact_attrs
                )
                if "time" in ds_metric.dims and not exact_match:  # and not exact_match
                    warnings.warn(
                        "This metric had a time dimension and did not perfectly match. Not parsing anything."
                    )
                else:
                    # parse the original key attr to the dataarray
                    ds_metric[mv].attrs["original_key"] = ds_metric.attrs[
                        "original_key"
                    ]
                    ds = parse_metric(
                        ds, ds_metric[mv], dim_length_conflict=dim_length_conflict
                    )
                    if exact_match:
                        exact_datasets[mv] += 1
                    else:
                        nonexact_datasets[mv] += 1

        ds_dict_parsed[k] = ds
    if print_statistics:
        print(
            f"Processed {total_datasets} datasets.\nExact matches:{exact_datasets}\nOther matches:{nonexact_datasets}\nNo match found:{nomatch_datasets}"
        )
    return ds_dict_parsed


def combine_datasets(
    ds_dict, combine_func, combine_func_kwargs={}, match_attrs=exact_attrs
):
    # make a copy of the input dict, so it is not modified outside of the function
    # ? Not sure this is always desired.
    ds_dict = {k: v for k, v in ds_dict.items()}
    ds_dict_combined = {}

    while len(ds_dict) > 0:
        # The order does not matter here, so just choose the first key
        k = list(ds_dict.keys())[0]
        ds = ds_dict.pop(k)
        matched_datasets = match_datasets(ds, ds_dict, match_attrs, pop=True)

        # create new dict key
        new_k = _key_from_attrs(ds, match_attrs, sep=".")

        # for now Ill hardcode the merging. I have been thinking if I should just pass a general `func` to deal with a list of datasets. The user could pass custom stuff
        # And I think that way I can generalize all/most of the `member` level combine functions. Well for now, lets do it the manual way.
        try:
            ds_combined = combine_func(matched_datasets, **combine_func_kwargs)
            ds_dict_merged[new_k] = ds_combined
        except:
            warnings.warn(f"{cmip6_dataset_id(ds)} failed to merge")

    return ds_dict_combined
