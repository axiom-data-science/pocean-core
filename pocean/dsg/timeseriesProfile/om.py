#!python
# coding=utf-8
from copy import copy
from datetime import datetime
from collections import OrderedDict

import six
import numpy as np
import pandas as pd
import netCDF4 as nc4

from pocean.utils import (
    dict_update,
    downcast_dataframe,
    generic_masked,
    get_dtype,
    normalize_array,
)
from pocean.cf import CFDataset
from pocean.cf import cf_safe_name
from pocean import logger  # noqa


class OrthogonalMultidimensionalTimeseriesProfile(CFDataset):

    @classmethod
    def is_mine(cls, dsg):
        try:
            assert dsg.featureType.lower() == 'timeseriesprofile'
            assert len(dsg.t_axes()) >= 1
            assert len(dsg.x_axes()) >= 1
            assert len(dsg.y_axes()) >= 1
            assert len(dsg.z_axes()) >= 1

            # If there is only a single set of levels and a single set of
            # times, then it is orthogonal.
            tvar = dsg.t_axes()[0]
            assert len(tvar.dimensions) == 1

            zvar = dsg.z_axes()[0]
            assert len(zvar.dimensions) == 1

            assert tvar.dimensions != zvar.dimensions

            # Not ragged
            o_index_vars = dsg.filter_by_attrs(
                sample_dimension=lambda x: x is not None
            )
            assert len(o_index_vars) == 0

            r_index_vars = dsg.filter_by_attrs(
                instance_dimension=lambda x: x is not None
            )
            assert len(r_index_vars) == 0

        except BaseException:
            return False

        return True

    @classmethod
    def from_dataframe(cls, df, output, **kwargs):
        reserved_columns = ['station', 't', 'x', 'y', 'z']
        data_columns = [ d for d in df.columns if d not in reserved_columns ]

        reduce_dims = kwargs.pop('reduce_dims', False)
        unlimited = kwargs.pop('unlimited', False)

        # Downcast anything from int64 to int32
        df = downcast_dataframe(df)

        # Make a new index that is the Cartesian product of all of the values from all of the
        # values of the old index. This is so don't have to iterate over anything. The full column
        # of data will be able to be shaped to the size of the final unique sized dimensions.
        index_order = ['t', 'z', 'station']
        df = df.set_index(index_order)
        df = df.reindex(
            pd.MultiIndex.from_product(df.index.levels, names=index_order)
        )

        unique_z = df.index.get_level_values('z').unique().values.astype(np.int32)
        unique_t = df.index.get_level_values('t').unique().tolist()  # tolist converts to datetime
        all_stations = df.index.get_level_values('station')
        unique_s = all_stations.unique()

        with OrthogonalMultidimensionalTimeseriesProfile(output, 'w') as nc:

            if reduce_dims is True and unique_s.size == 1:
                # If a singlular trajectory, we can reduce that dimension if it is of size 1
                def ts():
                    return np.s_[:, :]
                default_dimensions = ('time', 'z')
                station_dimensions = ()
            else:
                def ts():
                    return np.s_[:, :, :]
                default_dimensions = ('time', 'z', 'station')
                station_dimensions = ('station',)
                nc.createDimension('station', unique_s.size)

            station = nc.createVariable('station', get_dtype(unique_s), station_dimensions)
            latitude = nc.createVariable('latitude', get_dtype(df.y), station_dimensions)
            longitude = nc.createVariable('longitude', get_dtype(df.x), station_dimensions)
            # Assign over loop because VLEN variables (strings) have to be assigned by integer index
            # and we need to find the lat/lon based on station index
            for si, st in enumerate(unique_s):
                station[si] = st
                latitude[si] = df.y[all_stations == st].dropna().iloc[0]
                longitude[si] = df.x[all_stations == st].dropna().iloc[0]

            # Metadata variables
            nc.createVariable('crs', 'i4')

            # Create all of the variables
            if unlimited is True:
                nc.createDimension('time', None)
            else:
                nc.createDimension('time', len(unique_t))
            time = nc.createVariable('time', 'f8', ('time',))
            time[:] = nc4.date2num(unique_t, units=cls.default_time_unit)

            nc.createDimension('z', unique_z.size)
            z = nc.createVariable('z', get_dtype(unique_z), ('z',))
            z[:] = unique_z

            attributes = dict_update(nc.nc_attributes(), kwargs.pop('attributes', {}))

            for c in data_columns:
                # Create variable if it doesn't exist
                var_name = cf_safe_name(c)
                if var_name not in nc.variables:
                    if np.issubdtype(df[c].dtype, 'S') or df[c].dtype == object:
                        # AttributeError: cannot set _FillValue attribute for VLEN or compound variable
                        v = nc.createVariable(var_name, get_dtype(df[c]), default_dimensions)
                    else:
                        v = nc.createVariable(var_name, get_dtype(df[c]), default_dimensions, fill_value=df[c].dtype.type(cls.default_fill_value))

                    if var_name not in attributes:
                        attributes[var_name] = {}
                    attributes[var_name] = dict_update(attributes[var_name], {
                        'coordinates' : 'time latitude longitude z',
                    })
                else:
                    v = nc.variables[var_name]

                if hasattr(v, '_FillValue'):
                    vvalues = df[c].fillna(v._FillValue).values
                else:
                    # Use an empty string... better than nothing!
                    vvalues = df[c].fillna('').values

                v[ts()] = vvalues.reshape(len(unique_t), unique_z.size, unique_s.size)

            nc.update_attributes(attributes)

        return OrthogonalMultidimensionalTimeseriesProfile(output, **kwargs)

    def calculated_metadata(self, df=None, geometries=True, clean_cols=True, clean_rows=True):
        # if df is None:
        #     df = self.to_dataframe(clean_cols=clean_cols, clean_rows=clean_rows)
        raise NotImplementedError

    def to_dataframe(self, clean_cols=True, clean_rows=True):
        svar = self.filter_by_attrs(cf_role='timeseries_id')[0]
        try:
            s = normalize_array(svar)
            if isinstance(s, six.string_types):
                s = np.asarray([s])
        except ValueError:
            s = np.asarray(list(range(len(svar))), dtype=np.integer)
        n_stations = s.size

        # T
        tvar = self.t_axes()[0]
        t = nc4.num2date(tvar[:], tvar.units, getattr(tvar, 'calendar', 'standard'))
        if isinstance(t, datetime):
            # Size one
            t = np.array([t.isoformat()], dtype='datetime64')
        n_times = t.size

        # X
        xvar = self.x_axes()[0]
        x = generic_masked(xvar[:], attrs=self.vatts(xvar.name)).round(5)

        # Y
        yvar = self.y_axes()[0]
        y = generic_masked(yvar[:], attrs=self.vatts(yvar.name)).round(5)

        # Z
        zvar = self.z_axes()[0]
        z = generic_masked(zvar[:], attrs=self.vatts(zvar.name))
        n_z = z.size

        # denormalize table structure
        t = np.repeat(t, n_stations * n_z)
        z = np.tile(np.repeat(z, n_stations), n_times)
        s = np.tile(s, n_z * n_times)
        y = np.tile(y, n_times * n_z)
        x = np.tile(x, n_times * n_z)

        df_data = OrderedDict([
            ('t', t),
            ('x', x),
            ('y', y),
            ('z', z),
            ('station', s),
        ])

        extract_vars = copy(self.variables)
        del extract_vars[svar.name]
        del extract_vars[xvar.name]
        del extract_vars[yvar.name]
        del extract_vars[zvar.name]
        del extract_vars[tvar.name]

        building_index_to_drop = np.ones(t.size, dtype=bool)
        for i, (dnam, dvar) in enumerate(extract_vars.items()):
            if dvar[:].flatten().size != t.size:
                logger.warning("Variable {} is not the correct size, skipping.".format(dnam))
                continue

            vdata = generic_masked(dvar[:].flatten(), attrs=self.vatts(dnam))
            building_index_to_drop = (building_index_to_drop == True) & (vdata.mask == True)  # noqa
            if vdata.size == 1:
                vdata = vdata[0]
            df_data[dnam] = vdata

        df = pd.DataFrame(df_data)

        # Drop all data columns with no data
        if clean_cols:
            df = df.dropna(axis=1, how='all')

        # Drop all data rows with no data variable data
        if clean_rows:
            df = df.iloc[~building_index_to_drop]

        return df

    def nc_attributes(self):
        atts = super(OrthogonalMultidimensionalTimeseriesProfile, self).nc_attributes()
        return dict_update(atts, {
            'global' : {
                'featureType': 'timeSeriesProfile',
                'cdm_data_type': 'TimeseriesProfile'
            },
            'station' : {
                'cf_role': 'timeseries_id',
                'long_name' : 'station identifier'
            },
            'longitude': {
                'axis': 'X'
            },
            'latitude': {
                'axis': 'Y'
            },
            'z': {
                'axis': 'Z'
            },
            'time': {
                'units': self.default_time_unit,
                'standard_name': 'time',
                'axis': 'T'
            }
        })
