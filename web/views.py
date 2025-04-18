import json
from typing import List

import dash_bootstrap_components as dbc
from dash.dependencies import Output, Input, MATCH, State
from dash.exceptions import PreventUpdate
import dash_core_components as dcc
import dash_html_components as html
import dash_daq as daq
from flask import jsonify, request, Response as FlaskResponse

import web.utils
from libs.car import Cars, Car
from mylogger import logger

from trip import Trips

from libs.charging import Charging
from web import figures

from web.app import app, dash_app
from web.db import Database
from web.config_views import config_layout, config_otp_layout, log_layout
from web.utils import diff_dashtable, dash_date_to_datetime

# pylint: disable=invalid-name
from web.figurefilter import FigureFilter
from web.utils import create_card
from libs.config import Config

RESPONSE = "-response"
EMPTY_DIV = "empty-div"
ABRP_SWITCH = 'abrp-switch'
CALLBACK_CREATED = False

trips: Trips = Trips()
chargings: List[dict]
min_date = max_date = min_millis = max_millis = step = marks = cached_layout = None
CONFIG = Config()


@dash_app.callback(Output('page-content', 'children'),
                   [Input('url', 'pathname')])
def display_page(pathname):
    pathname = pathname[len(dash_app.requests_pathname_external_prefix)-1:]
    if pathname == "/config":
        return config_layout
    if pathname == "/log":
        return log_layout()
    if not CONFIG.is_good:
        return dcc.Location(pathname=dash_app.requests_pathname_external_prefix + "config", id="config_redirect")
    if pathname == "/config_otp":
        return config_otp_layout
    return serve_layout()


def create_callback():  # noqa: MC0001
    global CALLBACK_CREATED
    if not CALLBACK_CREATED:
        @dash_app.callback(Output(EMPTY_DIV, "children"),
                           [Input("battery-table", "data_timestamp")],
                           [State("battery-table", "data"),
                            State("battery-table", "data_previous")])
        def capture_diffs_in_battery_table(timestamp, data, data_previous):  # pylint: disable=unused-variable
            if timestamp is None:
                raise PreventUpdate
            diff_data = diff_dashtable(data, data_previous, "start_at")
            for changed_line in diff_data:
                if changed_line['column_name'] == 'price':
                    conn = Database.get_db()
                    if not Database.set_chargings_price(conn, dash_date_to_datetime(changed_line['start_at']),
                                                        changed_line['current_value']):
                        logger.error("Can't find line to update in the database")
                    else:
                        logger.debug("update price %s of %s", changed_line['current_value'], changed_line['start_at'])
                    conn.close()
            return ""  # don't need to update dashboard

        @dash_app.callback([Output("tab_battery_popup_graph", "children"), Output("tab_battery_popup", "is_open")],
                           [Input("battery-table", "active_cell"),
                            Input("tab_battery_popup-close", "n_clicks")],
                           [State('battery-table', 'data'),
                            State("tab_battery_popup", "is_open")])
        def get_battery_curve(active_cell, close, data, is_open):  # pylint: disable=unused-argument, unused-variable
            if is_open is None:
                is_open = False
            if active_cell is not None and active_cell["column_id"] in ["start_level", "end_level"] and not is_open:
                row = data[active_cell["row"]]
                return figures.get_battery_curve_fig(row, CONFIG.myp.vehicles_list[0]), True
            return "", False

        @dash_app.callback([Output("tab_trips_popup_graph", "children"), Output("tab_trips_popup", "is_open"), ],
                           [Input("trips-table", "active_cell"),
                            Input("tab_trips_popup-close", "n_clicks")],
                           State("tab_trips_popup", "is_open"))
        def get_altitude_graph(active_cell, close, is_open):  # pylint: disable=unused-argument, unused-variable
            if is_open is None:
                is_open = False
            if active_cell is not None and active_cell["column_id"] in ["altitude_diff"] and not is_open:
                return figures.get_altitude_fig(trips[active_cell["row_id"] - 1]), True
            return "", False

        CALLBACK_CREATED = True


@dash_app.callback(Output({'role': ABRP_SWITCH + RESPONSE, 'vin': MATCH}, 'children'),
                   Input({'role': ABRP_SWITCH, 'vin': MATCH}, 'id'),
                   Input({'role': ABRP_SWITCH, 'vin': MATCH}, 'value'))
def update_abrp(div_id, value):
    vin = div_id["vin"]
    if value:
        CONFIG.myp.abrp.abrp_enable_vin.add(vin)
    else:
        CONFIG.myp.abrp.abrp_enable_vin.discard(vin)
    CONFIG.myp.save_config()
    return " "


@app.route('/get_vehicles')
def get_vehicules():
    response = app.response_class(
        response=json.dumps(CONFIG.myp.get_vehicles(), default=lambda car: car.to_dict()),
        status=200,
        mimetype='application/json'
    )
    return response


@app.route('/get_vehicleinfo/<string:vin>')
def get_vehicle_info(vin):
    from_cache = int(request.args.get('from_cache', 0)) == 1
    response = app.response_class(
        response=json.dumps(CONFIG.myp.get_vehicle_info(vin, from_cache).to_dict(), default=str),
        status=200,
        mimetype='application/json'
    )
    return response


STYLE_CACHE = None


@app.route("/style.json")
def get_style():
    global STYLE_CACHE
    if not STYLE_CACHE:
        with open(app.root_path + "/assets/style.json", "r") as f:
            res = json.loads(f.read())
            STYLE_CACHE = res
    url_root = request.url_root
    STYLE_CACHE["sprite"] = url_root + "assets/sprites/osm-liberty@2x"
    return jsonify(STYLE_CACHE)


@app.route('/charge_now/<string:vin>/<int:charge>')
def charge_now(vin, charge):
    return jsonify(CONFIG.myp.charge_now(vin, charge != 0))


@app.route('/charge_hour')
def change_charge_hour():
    return jsonify(CONFIG.myp.change_charge_hour(request.form['vin'], request.form['hour'], request.form['minute']))


@app.route('/wakeup/<string:vin>')
def wakeup(vin):
    return jsonify(CONFIG.myp.wakeup(vin))


@app.route('/preconditioning/<string:vin>/<int:activate>')
def preconditioning(vin, activate):
    return jsonify(CONFIG.myp.preconditioning(vin, activate))


@app.route('/position/<string:vin>')
def get_position(vin):
    res = CONFIG.myp.get_vehicle_info(vin)
    try:
        coordinates = res.last_position.geometry.coordinates
    except AttributeError:
        return jsonify({'error': 'last_position not available from api'})
    longitude, latitude = coordinates[:2]
    if len(coordinates) == 3:  # altitude is not always available
        altitude = coordinates[2]
    else:
        altitude = None
    return jsonify(
        {"longitude": longitude, "latitude": latitude, "altitude": altitude,
         "url": f"https://maps.google.com/maps?q={latitude},{longitude}"})


# Set a battery threshold and schedule an hour to stop the charge
@app.route('/charge_control')
def get_charge_control():
    logger.info(request)
    vin = request.args['vin']
    charge_control = CONFIG.chc.get(vin)
    if charge_control is None:
        return jsonify("error: VIN not in list")
    if 'hour' in request.args and 'minute' in request.args:
        charge_control.set_stop_hour([int(request.args["hour"]), int(request.args["minute"])])
    if 'percentage' in request.args:
        charge_control.percentage_threshold = int(request.args['percentage'])
    CONFIG.chc.save_config()
    return jsonify(charge_control.get_dict())


@app.route('/positions')
def get_recorded_position():
    return FlaskResponse(Database.get_recorded_position(), mimetype='application/json')


@app.route('/abrp')
def abrp():
    vin = request.args.get('vin', None)
    enable = request.args.get('enable', None)
    token = request.args.get('token', None)
    if vin is not None and enable is not None:
        if enable == '1':
            CONFIG.myp.abrp.abrp_enable_vin.add(vin)
        else:
            CONFIG.myp.abrp.abrp_enable_vin.discard(vin)
    if token is not None:
        CONFIG.myp.abrp.token = token
    return jsonify(dict(CONFIG.myp.abrp))


@app.after_request
def after_request(response):
    header = response.headers
    header['Access-Control-Allow-Origin'] = '*'
    return response


def update_trips():
    global trips, chargings, cached_layout, min_date, max_date, min_millis, max_millis, step, marks
    logger.info("update_data")
    conn = Database.get_db(update_callback=False)
    Database.add_altitude_to_db(conn)
    conn.close()
    min_date = None
    max_date = None
    if CONFIG.is_good:
        car = CONFIG.myp.vehicles_list[0]  # todo handle multiple car
        try:
            trips_by_vin = Trips.get_trips(Cars([car]))
            trips = trips_by_vin[car.vin]
            assert len(trips) > 0
            min_date = trips[0].start_at
            max_date = trips[-1].start_at
            figures.get_figures(trips[0].car)
        except (AssertionError, KeyError):
            logger.debug("No trips yet")
            figures.get_figures(Car("vin", "vid", "brand"))
        try:
            chargings = Charging.get_chargings()
            assert len(chargings) > 0
            if min_date:
                min_date = min(min_date, chargings[0]["start_at"])
                max_date = max(max_date, chargings[-1]["start_at"])
            else:
                min_date = chargings[0]["start_at"]
                max_date = chargings[-1]["start_at"]
        except AssertionError:
            logger.debug("No chargings yet")
            if min_date is None:
                return
        # update for slider
        try:
            logger.debug("min_date:%s - max_date:%s", min_date, max_date)
            min_millis = web.utils.unix_time_millis(min_date)
            max_millis = web.utils.unix_time_millis(max_date)
            step = (max_millis - min_millis) / 100
            marks = web.utils.get_marks_from_start_end(min_date, max_date)
            cached_layout = None  # force regenerate layout
            figures.get_figures(car)
        except (ValueError, IndexError):
            logger.error("update_trips (slider): %s", exc_info=True)
        except AttributeError:
            logger.debug("position table is probably empty :", exc_info=True)
    return


def __get_control_tabs():
    tabs = []
    for car in CONFIG.myp.vehicles_list:
        if car.label is None:
            label = car.vin
        else:
            label = car.label
        # pylint: disable=not-callable
        tabs.append(dbc.Tab(label=label, id="tab-" + car.vin, children=[
            daq.ToggleSwitch(
                id={'role': ABRP_SWITCH, 'vin': car.vin},
                value=car.vin in CONFIG.myp.abrp.abrp_enable_vin,
                label="Send data to ABRP"
            ),
            html.Div(id={'role': ABRP_SWITCH + RESPONSE, 'vin': car.vin})
        ]))
    return tabs


def serve_layout():
    global cached_layout
    if cached_layout is None:
        logger.debug("Create new layout")
        fig_filter = FigureFilter()
        try:
            range_slider = dcc.RangeSlider(
                id='date-slider',
                min=min_millis,
                max=max_millis,
                step=step,
                marks=marks,
                value=[min_millis, max_millis],
            )
            summary_tab = [
                dbc.Container(dbc.Row(id="summary-cards",
                                      children=create_card(figures.SUMMARY_CARDS)), fluid=True),
                fig_filter.add_graph(dcc.Graph(id="consumption_fig"), "start_at", ["consumption_km"],
                                     figures.consumption_fig),
                fig_filter.add_graph(dcc.Graph(id="consumption_fig_by_speed"), "speed_average",
                                     ["consumption_km"] * 2, figures.consumption_fig_by_speed),
                fig_filter.add_graph(dcc.Graph(id="consumption_graph_by_temp"), "consumption_by_temp",
                                     ["consumption_km"] * 2, figures.consumption_fig_by_temp)]
            maps = fig_filter.add_map(dcc.Graph(id="trips_map", style={"height": '90vh'}), "lat",
                                      ["long", "start_at"], figures.trips_map)
            fig_filter.add_table("trips", figures.table_fig)
            fig_filter.add_table("chargings", figures.battery_table)
            fig_filter.src = {"trips": trips.get_trips_as_dict(), "chargings": chargings}
            fig_filter.set_clientside_callback(dash_app)
            create_callback()
        except (IndexError, TypeError, NameError, AssertionError, NameError, AttributeError):
            summary_tab = figures.ERROR_DIV
            maps = figures.ERROR_DIV
            logger.warning("Failed to generate figure, there is probably not enough data yet", exc_info_debug=True)
            range_slider = html.Div()
            figures.battery_table = figures.ERROR_DIV

        data_div = html.Div([
            *fig_filter.get_store(),
            html.Div([
                range_slider,
                dbc.Tabs([
                    dbc.Tab(label="Summary", tab_id="summary", children=summary_tab),
                    dbc.Tab(label="Trips", tab_id="trips", id="tab_trips",
                            children=[html.Div(id="tab_trips_fig", children=figures.table_fig),
                                      dbc.Modal(
                                          [
                                              dbc.ModalHeader("Altitude"),
                                              dbc.ModalBody(html.Div(
                                                  id="tab_trips_popup_graph")),
                                              dbc.ModalFooter(
                                                  dbc.Button("Close",
                                                             id="tab_trips_popup-close",
                                                             className="ml-auto")
                                              ),
                                          ],
                                          id="tab_trips_popup",
                                          size="xl",
                                      )
                                      ]),
                    dbc.Tab(label="Charge", tab_id="charge", id="tab_charge",
                            children=[figures.battery_table,
                                      dbc.Modal(
                                          [
                                              dbc.ModalHeader(
                                                  "Charging speed"),
                                              dbc.ModalBody(html.Div(
                                                  id="tab_battery_popup_graph")),
                                              dbc.ModalFooter(
                                                  dbc.Button("Close",
                                                             id="tab_battery_popup-close",
                                                             className="ml-auto")
                                              ),
                                          ],
                                          id="tab_battery_popup",
                                          size="xl",
                                      )
                                      ]),
                    dbc.Tab(label="Map", tab_id="map", children=[maps]),
                    dbc.Tab(label="Control", tab_id="control", children=dbc.Tabs(id="control-tabs",
                                                                                 children=__get_control_tabs()))],
                    id="tabs",
                    active_tab="summary",
                    persistence=True),
                html.Div(id=EMPTY_DIV),
                html.Div(id=EMPTY_DIV + "1")
            ])])
        cached_layout = data_div
    return cached_layout


try:
    Charging.set_default_price()
    Database.set_db_callback(update_trips)
    update_trips()
except (IndexError, TypeError):
    logger.debug("Failed to get trips, there is probably not enough data yet:", exc_info=True)

dash_app.layout = dbc.Container(fluid=True, children=[dcc.Location(id='url', refresh=False),
                                                      html.H1('My car info'), html.Div(id='page-content')])
