import cProfile
import os
import pstats
import warnings
from datetime import datetime
from functools import partial

import numpy as np
import pandas as pd
import pandas.api.types as pdtypes

from featuretools import variable_types
from featuretools.computational_backends.base_backend import (
    ComputationalBackend
)
from featuretools.computational_backends.feature_set import FeatureSet
from featuretools.exceptions import UnknownFeature
from featuretools.feature_base import (
    AggregationFeature,
    DirectFeature,
    GroupByTransformFeature,
    IdentityFeature,
    TransformFeature
)
from featuretools.utils import Trie, is_python_2
from featuretools.utils.gen_utils import get_relationship_variable_id

warnings.simplefilter('ignore', np.RankWarning)
warnings.simplefilter("ignore", category=RuntimeWarning)


class PandasBackend(ComputationalBackend):

    def __init__(self, entityset, features):
        assert len(set(f.entity.id for f in features)) == 1, \
            "Features must all be defined on the same entity"

        self.entityset = entityset
        self.target_eid = features[0].entity.id
        self.feature_set = FeatureSet(entityset, features)

    def __sizeof__(self):
        return self.entityset.__sizeof__()

    def calculate_all_features(self, instance_ids, time_last,
                               training_window=None, profile=False,
                               precalculated_features=None, ignored=None):
        """
        Given a list of instance ids and features with a shared time window,
        generate and return a mapping of instance -> feature values.

        Args:
            instance_ids (list): List of instance id for which to build features.

            time_last (pd.Timestamp): Last allowed time. Data from exactly this
                time not allowed.

            training_window (Timedelta, optional): Window defining how much time before the cutoff time data
                can be used when calculating features. If None, all data before cutoff time is used.

            profile (bool): Enable profiler if True.

            ignored (set[int]): Unique names of precalculated features.

            precalculated_features (dict[str -> pd.DataFrame]): Maps entity ids
                to dataframes containing precalculated features.

        Returns:
            pd.DataFrame : Pandas DataFrame of calculated feature values.
                Indexed by instance_ids. Columns in same order as features
                passed in.

        """
        calculator = _FeaturesCalculator(self.target_eid, self.entityset,
                                         self.feature_set, time_last, training_window,
                                         precalculated_features, ignored)
        return calculator.run(instance_ids, profile)


class _FeaturesCalculator(object):
    """
    A helper class to hold data that is shared across recursive calls of
    _calculate_features_for_entity.
    """
    def __init__(self, target_eid, entityset, feature_set, time_last,
                 training_window, precalculated_features, ignored):
        self.target_eid = target_eid
        self.entityset = entityset
        self.feature_set = feature_set
        self.training_window = training_window
        self.ignored = ignored

        if time_last is None:
            time_last = datetime.now()

        self.time_last = time_last

        if precalculated_features is None:
            precalculated_features = {}

        # Make sure precalculated features have id variable.
        # TODO: is this necessary?
        for entity_id, precalc_feature_values in precalculated_features.items():
            entity_id_var = self.entityset[entity_id].index
            if entity_id_var not in precalc_feature_values:
                precalc_feature_values[entity_id_var] = precalc_feature_values.index.values
                precalculated_features[entity_id] = precalc_feature_values

        self.precalculated_features = precalculated_features

    def run(self, instance_ids, profile):
        """
        Calculate values of features for the given instances of the target
        entity.

        Summary of algorithm:
        1. Construct a trie where the edges are relationships and each node
            contains a set of features for a single entity. See
            FeatureSet._build_feature_trie.
        2. Initialize a trie for storing dataframes.
        3. Traverse the trie using depth first search. At each node calculate
            the features and store the resulting dataframe in the dataframe
            trie (so that its values can be used by features which depend on
            these features). See _calculate_features_for_entity.
        4. Get the dataframe at the root of the trie (for the target entity) and
            return the columns corresponding to the requested features.
        """
        assert len(instance_ids) > 0, "0 instance ids provided"

        # For debugging
        if profile:
            pr = cProfile.Profile()
            pr.enable()

        feature_trie = self.feature_set.feature_trie

        df_trie = Trie()

        target_entity = self.entityset[self.target_eid]
        self._calculate_features_for_entity(entity_id=self.target_eid,
                                            feature_trie=feature_trie,
                                            df_trie=df_trie,
                                            filter_variable=target_entity.index,
                                            filter_values=instance_ids)

        # debugging
        if profile:
            pr.disable()
            ROOT_DIR = os.path.expanduser("~")
            prof_folder_path = os.path.join(ROOT_DIR, 'prof')
            if not os.path.exists(prof_folder_path):
                os.mkdir(prof_folder_path)
            with open(os.path.join(prof_folder_path, 'inst-%s.log' %
                                   list(instance_ids)[0]), 'w') as f:
                pstats.Stats(pr, stream=f).strip_dirs().sort_stats("cumulative", "tottime").print_stats()

        # The dataframe for the target entity should be stored at the root of
        # df_trie.
        df = df_trie[[]]

        if df.empty:
            return self.generate_default_df(instance_ids=instance_ids)

        # fill in empty rows with default values
        missing_ids = [i for i in instance_ids if i not in
                       df[target_entity.index]]
        if missing_ids:
            default_df = self.generate_default_df(instance_ids=missing_ids,
                                                  extra_columns=df.columns)
            df = df.append(default_df, sort=True)

        df.index.name = self.entityset[self.target_eid].index
        column_list = []
        for feat in self.feature_set.target_features:
            column_list.extend(feat.get_feature_names())
        return df[column_list]

    def _calculate_features_for_entity(self, entity_id, feature_trie, df_trie,
                                       filter_variable, filter_values,
                                       parent_relationship=None,
                                       ancestor_relationship_variables=None,
                                       parent_df=None):
        """
        Generate dataframes with features calculated for this node of the trie,
        and all descendant nodes. The dataframes will be stored in df_trie.

        Args:
            entity_id (str): The name of the entity to calculate features for.

            feature_trie (Trie): the trie with sets of features to calculate.
                The root contains features for the given entity.

            df_trie (Trie): a parallel trie for storing dataframes. The
                dataframe with features calculated will be placed in the root.

            filter_variable (str): The name of the variable to filter this
                dataframe by.

            filter_values (pd.Series): The values to filter the filter_variable
                to.

            parent_relationship (Relationship): The relationship through which
                this entity is linked to its parent in the trie. Should be a
                forward relationship from this entity to its parent, or else
                None.

            ancestor_relationship_variables (list[str]): The names of variables
                which link the parent entity to its ancestors. These variables
                will be copied into the dataframe for this entity so that it is
                also linked to all ancestors.

            parent_df (pd.DataFrame): The data for the parent entity. Only
                required if ancestor_relationship_variables is present.
        """

        # Step 1: Get a dataframe for the given entity, filtered by the given
        # conditions.

        features = [self.feature_set.features_by_name[fname] for fname in feature_trie[[]]]
        need_all_rows = any(f.primitive.uses_full_entity for f in features)
        if need_all_rows:
            query_variable = None
            query_values = None
        else:
            query_variable = filter_variable
            query_values = filter_values

        entity = self.entityset[entity_id]
        columns = _necessary_columns(entity, features)
        df = entity.query_by_values(query_values,
                                    variable_id=query_variable,
                                    columns=columns,
                                    time_last=self.time_last,
                                    training_window=self.training_window)

        # Step 2: Add variables to the dataframe linking it to all ancestors.

        new_ancestor_relationship_variables = []
        if parent_relationship:
            if ancestor_relationship_variables:
                assert parent_df is not None

                df, new_ancestor_relationship_variables = self._add_ancestor_relationship_variables(
                    df, parent_df, ancestor_relationship_variables, parent_relationship)

            # Add the variable linking this entity to its parent, so that
            # descendants get linked to the parent.
            new_ancestor_relationship_variables.append(parent_relationship.child_variable.id)

        # Step 3: Recurse on children.

        for edge, sub_trie in feature_trie.children():
            is_forward, relationship = edge
            if is_forward:
                sub_entity = relationship.parent_entity.id
                sub_filter_variable = relationship.parent_variable.id
                sub_filter_values = df[relationship.child_variable.id]
            else:
                sub_entity = relationship.child_entity.id
                sub_filter_variable = relationship.child_variable.id
                sub_filter_values = df[relationship.parent_variable.id]

            sub_df_trie = df_trie.get_node([edge])
            self._calculate_features_for_entity(
                entity_id=sub_entity,
                feature_trie=sub_trie,
                df_trie=sub_df_trie,
                filter_variable=sub_filter_variable,
                filter_values=sub_filter_values,
                parent_relationship=(not is_forward) and relationship,
                ancestor_relationship_variables=(not is_forward) and new_ancestor_relationship_variables,
                parent_df=(not is_forward) and df)

        # Step 4: Calculate the features for this entity.
        #
        # All dependencies of the features for this entity have been calculated
        # by the above recursive calls, and their results stored in df_trie.

        # Add any precalculated features.
        if entity_id in self.precalculated_features:
            precalc_feature_values = self.precalculated_features[entity_id]
            # Left outer merge to keep all rows of df.
            df = df.merge(precalc_feature_values,
                          how='left',
                          left_index=True,
                          right_index=True,
                          suffixes=('', '_precalculated'))

        feature_names = feature_trie[[]]
        if self.ignored:
            feature_names -= self.ignored

        # Group the features so that each group can be calculated together.
        # The groups must also be in topological order (if A is a transform of B
        # then B must be in a group before A).
        feature_groups = self.feature_set.group_features(feature_names)

        for group in feature_groups:
            representative_feature = group[0]
            handler = self._feature_type_handler(representative_feature)
            df = handler(group, df, df_trie)

        # If we used all rows, filter the df to those that we actually need.
        if need_all_rows:
            df = df[df[filter_variable].isin(filter_values)]

        # Step 5: Store the dataframe for this entity at the root of df_trie, so
        # that it can be accessed by the caller.
        df_trie[[]] = df

    def _add_ancestor_relationship_variables(self, child_df, parent_df,
                                             ancestor_relationship_variables,
                                             relationship):
        """
        Merge ancestor_relationship_variables from parent_df into child_df, adding a prefix to
        each column name specifying the relationship.

        Return the updated df and the new relationship variable names.

        Args:
            child_df (pd.DataFrame): The dataframe to add relationship variables to.
            parent_df (pd.DataFrame): The dataframe to copy relationship variables from.
            ancestor_relationship_variables (list[str]): The names of
                relationship variables in the parent_df to copy into child_df.
            relationship (Relationship): the relationship through which the
                child is connected to the parent.
        """
        relationship_name = relationship.parent_name
        new_relationship_variables = ['%s.%s' % (relationship_name, var)
                                      for var in ancestor_relationship_variables]

        # create an intermediate dataframe which shares a column
        # with the child dataframe and has a column with the
        # original parent's id.
        col_map = {relationship.parent_variable.id: relationship.child_variable.id}
        for child_var, parent_var in zip(new_relationship_variables, ancestor_relationship_variables):
            col_map[parent_var] = child_var

        merge_df = parent_df[list(col_map.keys())].rename(columns=col_map)

        merge_df.index.name = None  # change index name for merge

        # Merge the dataframe, adding the relationship variables to the child.
        # Left outer join so that all rows in child are kept (if it contains
        # all rows of the entity then there may not be corresponding rows in the
        # parent_df).
        df = child_df.merge(merge_df,
                            how='left',
                            left_on=relationship.child_variable.id,
                            right_on=relationship.child_variable.id)

        return df, new_relationship_variables

    def generate_default_df(self, instance_ids, extra_columns=None):
        default_row = []
        default_cols = []
        for f in self.feature_set.target_features:
            for name in f.get_feature_names():
                default_cols.append(name)
                default_row.append(f.default_value)

        default_matrix = [default_row] * len(instance_ids)
        default_df = pd.DataFrame(default_matrix,
                                  columns=default_cols,
                                  index=instance_ids)
        index_name = self.entityset[self.target_eid].index
        default_df.index.name = index_name
        if extra_columns is not None:
            for c in extra_columns:
                if c not in default_df.columns:
                    default_df[c] = [np.nan] * len(instance_ids)
        return default_df

    def _feature_type_handler(self, f):
        if type(f) == TransformFeature:
            return self._calculate_transform_features
        elif type(f) == GroupByTransformFeature:
            return self._calculate_groupby_features
        elif type(f) == DirectFeature:
            return self._calculate_direct_features
        elif type(f) == AggregationFeature:
            return self._calculate_agg_features
        elif type(f) == IdentityFeature:
            return self._calculate_identity_features
        else:
            raise UnknownFeature(u"{} feature unknown".format(f.__class__))

    def _calculate_identity_features(self, features, df, _df_trie):
        for f in features:
            assert f.get_name() in df, (
                'Column "%s" missing frome dataframe' % f.get_name())

        return df

    def _calculate_transform_features(self, features, frame, _df_trie):
        for f in features:
            # handle when no data
            if frame.shape[0] == 0:
                set_default_column(frame, f)
                continue

            # collect only the variables we need for this transformation
            variable_data = [frame[bf.get_name()]
                             for bf in f.base_features]

            feature_func = f.get_function()
            # apply the function to the relevant dataframe slice and add the
            # feature row to the results dataframe.
            if f.primitive.uses_calc_time:
                values = feature_func(*variable_data, time=self.time_last)
            else:
                values = feature_func(*variable_data)

            # if we don't get just the values, the assignment breaks when indexes don't match
            if f.number_output_features > 1:
                values = [strip_values_if_series(value) for value in values]
            else:
                values = [strip_values_if_series(values)]
            update_feature_columns(f, frame, values)

        return frame

    def _calculate_groupby_features(self, features, frame, _df_trie):
        for f in features:
            set_default_column(frame, f)

        # handle when no data
        if frame.shape[0] == 0:
            return frame

        groupby = features[0].groupby.get_name()
        for index, group in frame.groupby(groupby):
            for f in features:
                column_names = [bf.get_name() for bf in f.base_features]
                # exclude the groupby variable from being passed to the function
                variable_data = [group[name] for name in column_names[:-1]]
                feature_func = f.get_function()

                # apply the function to the relevant dataframe slice and add the
                # feature row to the results dataframe.
                if f.primitive.uses_calc_time:
                    values = feature_func(*variable_data, time=self.time_last)
                else:
                    values = feature_func(*variable_data)

                # make sure index is aligned
                if isinstance(values, pd.Series):
                    values.index = variable_data[0].index
                else:
                    values = pd.Series(values, index=variable_data[0].index)

                feature_name = f.get_name()
                frame[feature_name].update(values)

        return frame

    def _calculate_direct_features(self, features, child_df, df_trie):
        path = features[0].relationship_path
        assert len(path) == 1, \
            "Error calculating DirectFeatures, len(path) != 1"

        relationship = path[0]
        parent_df = df_trie[[(True, relationship)]]
        merge_var = relationship.child_variable.id

        # generate a mapping of old column names (in the parent entity) to
        # new column names (in the child entity) for the merge
        col_map = {relationship.parent_variable.id: merge_var}
        index_as_feature = None
        for f in features:
            if f.base_features[0].get_name() == relationship.parent_variable.id:
                index_as_feature = f
            base_names = f.base_features[0].get_feature_names()
            for name, base_name in zip(f.get_feature_names(), base_names):
                if name in child_df.columns:
                    continue
                col_map[base_name] = name

        # merge the identity feature from the parent entity into the child
        merge_df = parent_df[list(col_map.keys())].rename(columns=col_map)
        if index_as_feature is not None:
            merge_df.set_index(index_as_feature.get_name(), inplace=True,
                               drop=False)
        else:
            merge_df.set_index(merge_var, inplace=True)

        new_df = child_df.merge(merge_df, left_on=merge_var, right_index=True,
                                how='left')

        return new_df

    def _calculate_agg_features(self, features, frame, df_trie):
        test_feature = features[0]
        child_entity = test_feature.base_features[0].entity

        path = [(False, r) for r in test_feature.relationship_path]
        base_frame = df_trie[path]
        # Sometimes approximate features get computed in a previous filter frame
        # and put in the current one dynamically,
        # so there may be existing features here
        features = [f for f in features if f.get_name()
                    not in frame.columns]
        if not len(features):
            return frame

        # handle where
        where = test_feature.where
        if where is not None and not base_frame.empty:
            base_frame = base_frame.loc[base_frame[where.get_name()]]

        # when no child data, just add all the features to frame with nan
        if base_frame.empty:
            for f in features:
                frame[f.get_name()] = np.nan
        else:
            relationship_path = test_feature.relationship_path

            groupby_var = get_relationship_variable_id(relationship_path)

            # if the use_previous property exists on this feature, include only the
            # instances from the child entity included in that Timedelta
            use_previous = test_feature.use_previous
            if use_previous and not base_frame.empty:
                # Filter by use_previous values
                time_last = self.time_last
                if use_previous.is_absolute():
                    time_first = time_last - use_previous
                    ti = child_entity.time_index
                    if ti is not None:
                        base_frame = base_frame[base_frame[ti] >= time_first]
                else:
                    n = use_previous.value

                    def last_n(df):
                        return df.iloc[-n:]

                    base_frame = base_frame.groupby(groupby_var, observed=True, sort=False).apply(last_n)

            to_agg = {}
            agg_rename = {}
            to_apply = set()
            # apply multivariable and time-dependent features as we find them, and
            # save aggregable features for later
            for f in features:
                if _can_agg(f):
                    variable_id = f.base_features[0].get_name()

                    if variable_id not in to_agg:
                        to_agg[variable_id] = []

                    func = f.get_function()

                    # for some reason, using the string count is significantly
                    # faster than any method a primitive can return
                    # https://stackoverflow.com/questions/55731149/use-a-function-instead-of-string-in-pandas-groupby-agg
                    if is_python_2() and func == pd.Series.count.__func__:
                        func = "count"
                    elif func == pd.Series.count:
                        func = "count"

                    funcname = func
                    if callable(func):
                        # if the same function is being applied to the same
                        # variable twice, wrap it in a partial to avoid
                        # duplicate functions
                        funcname = str(id(func))
                        if u"{}-{}".format(variable_id, funcname) in agg_rename:
                            func = partial(func)
                            funcname = str(id(func))

                        func.__name__ = funcname

                    to_agg[variable_id].append(func)
                    # this is used below to rename columns that pandas names for us
                    agg_rename[u"{}-{}".format(variable_id, funcname)] = f.get_name()
                    continue

                to_apply.add(f)

            # Apply the non-aggregable functions generate a new dataframe, and merge
            # it with the existing one
            if len(to_apply):
                wrap = agg_wrapper(to_apply, self.time_last)
                # groupby_var can be both the name of the index and a column,
                # to silence pandas warning about ambiguity we explicitly pass
                # the column (in actuality grouping by both index and group would
                # work)
                to_merge = base_frame.groupby(base_frame[groupby_var], observed=True, sort=False).apply(wrap)
                frame = pd.merge(left=frame, right=to_merge,
                                 left_index=True,
                                 right_index=True, how='left')

            # Apply the aggregate functions to generate a new dataframe, and merge
            # it with the existing one
            if len(to_agg):
                # groupby_var can be both the name of the index and a column,
                # to silence pandas warning about ambiguity we explicitly pass
                # the column (in actuality grouping by both index and group would
                # work)
                to_merge = base_frame.groupby(base_frame[groupby_var],
                                              observed=True, sort=False).agg(to_agg)
                # rename columns to the correct feature names
                to_merge.columns = [agg_rename["-".join(x)] for x in to_merge.columns.ravel()]
                to_merge = to_merge[list(agg_rename.values())]

                # workaround for pandas bug where categories are in the wrong order
                # see: https://github.com/pandas-dev/pandas/issues/22501
                if pdtypes.is_categorical_dtype(frame.index):
                    categories = pdtypes.CategoricalDtype(categories=frame.index.categories)
                    to_merge.index = to_merge.index.astype(object).astype(categories)

                frame = pd.merge(left=frame, right=to_merge,
                                 left_index=True, right_index=True, how='left')

        # Handle default values
        fillna_dict = {}
        for f in features:
            feature_defaults = {name: f.default_value
                                for name in f.get_feature_names()}
            fillna_dict.update(feature_defaults)

        frame.fillna(fillna_dict, inplace=True)

        # convert boolean dtypes to floats as appropriate
        # pandas behavior: https://github.com/pydata/pandas/issues/3752
        for f in features:
            if (f.number_output_features == 1 and
                    f.variable_type == variable_types.Numeric and
                    frame[f.get_name()].dtype.name in ['object', 'bool']):
                frame[f.get_name()] = frame[f.get_name()].astype(float)

        return frame


def _necessary_columns(entity, features):
    # We have to keep all Id columns because we don't know what forward
    # relationships will come from this node.
    index_columns = {v.id for v in entity.variables
                     if isinstance(v, (variable_types.Index,
                                       variable_types.Id,
                                       variable_types.TimeIndex))}
    feature_columns = {f.variable.id for f in features
                       if isinstance(f, IdentityFeature)}
    return index_columns | feature_columns


def _can_agg(feature):
    assert isinstance(feature, AggregationFeature)
    base_features = feature.base_features
    if feature.where is not None:
        base_features = [bf.get_name() for bf in base_features
                         if bf.get_name() != feature.where.get_name()]

    if feature.primitive.uses_calc_time:
        return False
    single_output = feature.primitive.number_output_features == 1
    return len(base_features) == 1 and single_output


def agg_wrapper(feats, time_last):
    def wrap(df):
        d = {}
        for f in feats:
            func = f.get_function()
            variable_ids = [bf.get_name() for bf in f.base_features]
            args = [df[v] for v in variable_ids]

            if f.primitive.uses_calc_time:
                values = func(*args, time=time_last)
            else:
                values = func(*args)

            if f.number_output_features == 1:
                values = [values]
            update_feature_columns(f, d, values)

        return pd.Series(d)
    return wrap


def set_default_column(frame, f):
    for name in f.get_feature_names():
        frame[name] = f.default_value


def update_feature_columns(feature, data, values):
    names = feature.get_feature_names()
    assert len(names) == len(values)
    for name, value in zip(names, values):
        data[name] = value


def strip_values_if_series(values):
    if isinstance(values, pd.Series):
        values = values.values
    return values
