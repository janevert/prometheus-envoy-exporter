# -*- coding: utf-8 -*-
#
#    envoy-exporter, a python script to query an Enphase Envoy device and export information to prometheus.
#
#    Copyright (C) 2021, Jan Evert van Grootheest
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

import argparse
import json
import logging
import os
import re
import requests
import sys
import time
from enum import Enum

from http.server import ThreadingHTTPServer
import jinja2
from prometheus_client import Info
from prometheus_client import Gauge
from prometheus_client import MetricsHandler

last_envoy_update_time = 0

unknown_statuses = []

# map from serial -> device information
devices = {}

envoy_address = 'envoy'
prometheus_port = 9101
# update data from envoy only if last request more than this # seconds ago
envoy_minimum_interval_seconds = 10

exporter_server = None
metrics_handler = None

jinja_environment = jinja2.Environment(autoescape=True)
homepage_template = None
last_data_template = None

homepage_template_source = '<html><head><style>' \
                           'table, th, td { border: 1px solid black; border-collapse: collapse; padding: 5px;}' \
                           '</style></head>' \
                           '<body><h1>Enphase Envoy Prometheus Exporter main</h1>' \
                           'For prometheus metrics, have a look at <a href="/metrics">/metrics</a>.' \
                           '<h2>Envoy</h2>' \
                           '<table>' \
                           '<tr><th>Serial</th><th>Part number</th><th>Installed date</th><th>Image loaded date</th></tr>' \
                           '{% for device in envoys %}' \
                           '<tr><td>{{ device["serial_num"] }}</td><td>{{ device["part_num"] }}</td><td>{{ device["installed_date_date"] }}</td><td>{{ device["image_loaded_date_date"] }}</td></tr>' \
                           '{% endfor %}' \
                           '</table>' \
                           '<h2>Inverters</h2>' \
                           '<table>' \
                           '<tr><th>Serial</th><th>Part number</th><th>Installed date</th><th>Image loaded date</th></tr>' \
                           '{% for device in converters %}' \
                           '<tr><td>{{ device["serial_num"] }}</td><td>{{ device["part_num"] }}</td><td>{{ device["installed_date_date"] }}</td><td>{{ device["image_loaded_date_date"] }}</td></tr>' \
                           '{% endfor %}' \
                           '</table>' \
                           '<h2>Development</h2>' \
                           '<ul><li><a href="/last_inventory">last received inventory response</a></li>' \
                           '<li><a href="/last_production">last received production response</a></li></ul>' \
                           '</body></html>'

last_data_template_source = '<html><body>' \
                            '<h1>{{title}}</h1>' \
                            '<p>request duration: {{request_length}} seconds</p>' \
                            '<h2>response:</h2>' \
                            '<pre>{{json_text}}</pre>' \
                            '</body></html>'


# Inverter production exported items
production_inverters_active_exporter = Gauge('envoy_production_inverters_active_count', 'Active inverter count.')
production_inverters_watt_now_exporter = Gauge('envoy_production_inverters_watt_now', 'Watt produced now.')
production_inverters_watt_hour_lifetime_exporter = Gauge('envoy_production_inverters_watt_hour_lifetime', 'Watt/hour produced over the lifetime.')

envoy_data_request_duration_exporter = Gauge('envoy_data_request_duration_seconds', 'The total time it took to request data from the envoy, in seconds.')
envoy_inventory_request_duration_exporter = Gauge('envoy_inventory_request_duration_seconds', 'The time it took to request inventory from the envoy, in seconds')
envoy_production_request_duration_exporter = Gauge('envoy_production_request_duration_seconds', 'The time it took to request production data from the envoy, in seconds')
envoy_inventory_request_failed_exporter = Gauge('envoy_inventory_request_failed_count', 'Sequential counter of failed requests, reset on successful request.')
envoy_production_request_failed_exporter = Gauge('envoy_production_request_failed_count', 'Sequential counter of failed requests, reset on successful request.')

class DeviceData(object):
    class DeviceType(Enum):
        INVERTER = 1
        AC_BATTERY = 2
        NSRB = 3

    # prometheus items
    metadata_exporter = Info('envoy_device_metadata', 'metadata.', ['serial_num'])
    producing_exporter = Gauge('envoy_device_producing', 'Indicates whether the device is producing data.',
                               ['serial_num'])
    communicating_exporter = Gauge('envoy_device_communicating',
                                   'Indicates whether the device is communicating.', ['serial_num'])
    provisioned_exporter = Gauge('envoy_device_provisioned', 'Indicates whether the device is provisioned.',
                                 ['serial_num'])
    operating_exporter = Gauge('envoy_device_operating', 'Indicates whether the device is operating.',
                               ['serial_num'])

    def __init__(self, new_type, new_inventory):
        # mostly static data
        self.type = new_type
        self.serial_num = new_inventory['serial_num']
        logging.debug('Creating DeviceData for type %s, serial %s' % (self.type, self.serial_num))
        self.part_num = new_inventory['part_num']
        self.installed_date = new_inventory['installed']
        self.installed_date_date = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(self.installed_date)))

        # dynamic data (image_loaded probably changes with a SW update)
        self.image_loaded_date = new_inventory['img_load_date']
        self.image_loaded_date_date = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(self.image_loaded_date)))
        self.device_status = []

        self.my_metadata_exporter = self.metadata_exporter.labels(serial_num=self.serial_num)
        self.my_producing_exporter = self.producing_exporter.labels(serial_num=self.serial_num)
        self.my_communicating_exporter = self.communicating_exporter.labels(serial_num=self.serial_num)
        self.my_provisioned_exporter = self.provisioned_exporter.labels(serial_num=self.serial_num)
        self.my_operating_exporter = self.operating_exporter.labels(serial_num=self.serial_num)

        self.my_metadata_exporter.info({'part_number': self.part_num,
                                        'installed_date': self.installed_date_date,
                                        'image_loaded_date': self.image_loaded_date_date})

    def update_inventory_data(self, new_inventory):
        # this doesn't happen too often
        if self.image_loaded_date != new_inventory['img_load_date']:
            self.image_loaded_date = new_inventory['img_load_date']
            self.image_loaded_date_date = time.strftime('%Y-%m-%d %H:%M:%S',
                                                        time.localtime(int(self.image_loaded_date)))
            self.my_metadata_exporter.info({'part_number': self.part_num,
                                            'installed_date': self.installed_date_date,
                                            'image_loaded_date': self.image_loaded_date_date})

        self.my_producing_exporter.set(new_inventory['producing'])
        self.my_communicating_exporter.set(new_inventory['communicating'])
        self.my_provisioned_exporter.set(new_inventory['provisioned'])
        self.my_operating_exporter.set(new_inventory['operating'])

    def update_production_data(self, direction, new_production):
        pass


class RelayDevice(DeviceData):

    # prometheus exported items
    line_count_exporter = Gauge('envoy_relay_line_count', 'Number of enabled lines.', ['serial_num'])
    line_connected_exporter = Gauge('envoy_relay_line_connected', 'Number of enabled lines.', ['serial_num', 'line'])
    watt_now_exporter = Gauge('envoy_relay_watt_now', 'Number of enabled lines.', ['serial_num', 'line', 'direction'])
    rms_current_exporter = Gauge('envoy_relay_rms_current', 'Number of enabled lines.', ['serial_num', 'line', 'direction'])
    rms_voltage_exporter = Gauge('envoy_relay_rms_voltage', 'Number of enabled lines.', ['serial_num', 'line', 'direction'])
    apparent_power_exporter = Gauge('envoy_relay_apparent_power', 'Number of enabled lines.', ['serial_num', 'line', 'direction'])
    power_factor_exporter = Gauge('envoy_relay_power_factor', 'Number of enabled lines.', ['serial_num', 'line', 'direction'])

    def __init__(self, new_type, new_inventory):
        super().__init__(new_type, new_inventory)

        self.line_count_exporter.labels(serial_num=self.serial_num).set(new_inventory['line-count'])

        relay_lines = 0
        self.line_connected = []
        for item in new_inventory:
            match = re.search('line(\d+)-connected', item)
            if match is not None:
                line_number = int(match.group(1))
                line_index = line_number - 1
                connected = bool(new_inventory[item])
                logging.debug('exporting line-connected for %s (relay_lines %s): %s' % (line_number, relay_lines, connected))
                relay_lines += 1
                self.line_connected.insert(line_index, connected)
                self.line_connected_exporter.labels(serial_num=self.serial_num, line=line_number).set(connected)
                if connected:
                    for direction in ['production', 'consumption']:
                        self.watt_now_exporter.labels(serial_num=self.serial_num, line=line_number, direction=direction)
                        self.rms_current_exporter.labels(serial_num=self.serial_num, line=line_number, direction=direction)
                        self.rms_voltage_exporter.labels(serial_num=self.serial_num, line=line_number, direction=direction)
                        self.apparent_power_exporter.labels(serial_num=self.serial_num, line=line_number, direction=direction)
                        self.power_factor_exporter.labels(serial_num=self.serial_num, line=line_number, direction=direction)

        if len(self.line_connected) != relay_lines:
            raise RuntimeError('relay lines are not sequential? line_connected %d, relay_lines %d' % (len(self.line_connected), relay_lines))

    def update_production_data(self, direction, new_production):
        # it looks like json['production']['type' == 'eim']['lines'] and json['consumption']['type' == 'eim']['lines']
        # contain the same values (or really very close to the same values), but we export both anyway
        for line_index in range(0, len(self.line_connected)):
            if self.line_connected[line_index]:
                line_number = line_index + 1
                line_info = new_production['lines'][line_index]
                self.watt_now_exporter.labels(serial_num=self.serial_num, line=line_number, direction=direction).set(line_info['wNow'])
                self.rms_current_exporter.labels(serial_num=self.serial_num, line=line_number, direction=direction).set(line_info['rmsCurrent'])
                self.rms_voltage_exporter.labels(serial_num=self.serial_num, line=line_number, direction=direction).set(line_info['rmsVoltage'])
                self.apparent_power_exporter.labels(serial_num=self.serial_num, line=line_number, direction=direction).set(line_info['apprntPwr'])
                self.power_factor_exporter.labels(serial_num=self.serial_num, line=line_number, direction=direction).set(line_info['pwrFactor'])

        super().update_production_data(direction, new_production)


class ExporterRequestHandler(MetricsHandler):
    """ Class handling requests from a browser or prometheus. """

    def do_GET(self):
        start = time.time()
        logging.info('GET %s' % self.path)
        logging.debug('Headers:\n%s\n' % self.headers)

        if self.path == '/':
            self.do_homepage()
        elif self.path == '/metrics':
            request_envoy_data()
            super().do_GET()
        elif self.path == '/last_inventory' or self.path == '/last_production':
            self.do_last_data()
        else:
            self.close_connection = True
            self.send_error(requests.codes.not_found)

        end = time.time()
        logging.info('GET %s finished in %.3fs.' % (self.path, end - start))

    def do_homepage(self):
        global homepage_template

        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()

        if not homepage_template:
            homepage_template = jinja_environment.from_string(homepage_template_source)

        page_data = homepage_template.render(envoys=(k for k in devices.values() if k.type == DeviceData.DeviceType.NSRB),
                                             converters=(k for k in devices.values() if k.type == DeviceData.DeviceType.INVERTER))
        self.wfile.write(page_data.encode('utf-8'))

    def do_last_data(self):
        global last_data_template

        request_envoy_data()

        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()

        if not last_data_template:
            last_data_template = jinja_environment.from_string(last_data_template_source)

        data = inventory_data if (self.path == '/last_inventory') else production_data

        request_length = '%0.03f' % data.last_update_duration
        last_data = json.dumps(data.last_json, indent=4)
        page_data = last_data_template.render(title=data.name, request_length=request_length, json_text=last_data)
        self.wfile.write(page_data.encode('utf-8'))


class EnvoyRequest(object):

    def __init__(self, name, address):
        self.name = name
        self.request_address = address

        self.last_text = None
        self.last_json = None
        self.last_update_time = 0
        self.last_update_duration = 0
        self.failed_request_count = 0
        self.my_failed_request_exporter = None

    def convert_data(self):
        """ Data specific converter. """
        pass

    def update(self):
        logging.debug('Requesting %s from envoy' % self.name)
        request_start = time.time()
        response = None
        try:
            response = requests.get("http://%s/%s" % (envoy_address, self.request_address), allow_redirects=False, timeout=3)
        except Exception as e:
            logging.error('Failed to request %s from envoy: %s' % (self.name, str(e)))
            self.failed_request_count += 1
            self.my_failed_request_exporter.set(self.failed_request_count)
            return
        self.failed_request_count = 0
        self.my_failed_request_exporter.set(self.failed_request_count)
        request_end = time.time()
        logging.info('%s response from envoy. status code %d, length %d, duration %.3fs' % (self.name, response.status_code, len(response.content), request_end - request_start))
        if response.status_code != requests.codes.ok:
            logging.warning('Failed to fetch %s from envoy because %s' % (self.name, response.reason))
            logging.debug('response headers:\n%s' % response.headers)
        else:
            self.last_text = response.text
            self.last_json = response.json()
            self.last_update_duration = request_end - request_start
            self.last_update_time = request_end
            self.convert_data()

    def find_device_by_serial(self, serial_num, device_type, inventory):
        """
        Find a device by serial. If device is not found and inventory is available, a new device is created.

        :param serial_num: The serial number of the device to find.
        :param device_type: The device type to use when created a device.
        :param inventory: The inventory to use when creating a device.
        :return:
        """
        if serial_num in devices:
            return devices[serial_num]

        if inventory is None:
            return None

        if device_type == DeviceData.DeviceType.NSRB:
            new_device = RelayDevice(device_type, inventory)
        else:
            new_device = DeviceData(device_type, inventory)
        devices[serial_num] = new_device
        logging.info('Created new DeviceData for %s' % serial_num)
        return new_device

    def find_device_by_type(self, device_type):
        """
        Find a device by type.

        :param device_type: The device type to use when created a device.
        :return:
        """

        for device in devices.values():
            if device.type == device_type:
                return device

        return None


class InventoryRequest(EnvoyRequest):
    # I think PCU == Power Converter Unit
    TYPE_INVERTER = 'PCU'

    # I don't have these devices, I suspect this is related to battery packs
    # ABC probably is "AC Batteries"
    TYPE_CHARGER = 'ACB'

    # no idea what NSRB means; judging by the serial, it is NOT the envoy itself
    # judging by the content of the inventory, this is some form of power switch, with relay and lines
    # N is Network? As in power network
    # S is Switch?
    # R is Relay?
    # B is Block?
    TYPE_SWITCH = 'NSRB'

    def __init__(self):
        super().__init__('inventory', 'inventory.json')
        self.my_failed_request_exporter = envoy_inventory_request_failed_exporter

    def convert_data(self):
        envoy_inventory_request_duration_exporter.set(self.last_update_duration)
        for item in self.last_json:
            # all are expected to have type, but lets make sure...
            if 'type' not in item:
                continue

            if item['type'] == self.TYPE_INVERTER:
                for device_inventory in item['devices']:
                    serial = device_inventory['serial_num']
                    device = self.find_device_by_serial(serial, DeviceData.DeviceType.INVERTER, device_inventory)
                    device.update_inventory_data(device_inventory)
            if item['type'] == self.TYPE_CHARGER:
                for device_inventory in item['devices']:
                    serial = device_inventory['serial_num']
                    device = self.find_device_by_serial(serial, DeviceData.DeviceType.AC_BATTERY, device_inventory)
                    device.update_inventory_data(device_inventory)
            if item['type'] == self.TYPE_SWITCH:
                for device_inventory in item['devices']:
                    serial = device_inventory['serial_num']
                    device = self.find_device_by_serial(serial, DeviceData.DeviceType.NSRB, device_inventory)
                    device.update_inventory_data(device_inventory)

        super().convert_data()


class ProductionRequest(EnvoyRequest):
    DIRECTION_PRODUCTION = 'production'
    DIRECTION_CONSUMPTION = 'consumption'

    TYPE_INVERTER = 'inverters'

    # these have information for 3 lines, so are related to the NSRB type in inventory
    TYPE_SWITCH = 'eim'

    def __init__(self):
        super().__init__('production', 'production.json?details=1')
        self.my_failed_request_exporter = envoy_production_request_failed_exporter

    def convert_data(self):
        envoy_production_request_duration_exporter.set(self.last_update_duration)
        for direction in [self.DIRECTION_PRODUCTION, self.DIRECTION_CONSUMPTION]:
            for item in self.last_json[direction]:
                # all are expected to have type, but lets make sure...
                if 'type' not in item:
                    continue

                if item['type'] == self.TYPE_INVERTER:
                    production_inverters_active_exporter.set(item['activeCount'])
                    production_inverters_watt_now_exporter.set(item['wNow'])
                    production_inverters_watt_hour_lifetime_exporter.set(item['whLifetime'])
                elif item['type'] == self.TYPE_SWITCH:
                    device = self.find_device_by_type(DeviceData.DeviceType.NSRB)
                    device.update_production_data(direction, item)

        super().convert_data()


inventory_data = InventoryRequest()
production_data = ProductionRequest()
# meter_data = EnvoyData('meter', 'ivp/meters')


def request_envoy_data():
    global last_envoy_update_time

    if last_envoy_update_time + envoy_minimum_interval_seconds > time.time():
        logging.debug('Not requesting information from envoy, too quick.')
        return

    # update time even when failed, so we don't spam the envoy
    last_envoy_update_time = time.time()

    start = time.time()
    inventory_data.update()
    production_data.update()
    # meter_data.update()
    end = time.time()

    envoy_data_request_duration_exporter.set(end - start)
    logging.info('Requesting information from envoy done in %.3fs.' % (end - start))


def start_exporter():
    global envoy_address, prometheus_port
    global exporter_server, metrics_handler
    global jinja_environment

    parser = argparse.ArgumentParser(description='Prometheus exporter for information from Enphase Envoy.')
    parser.add_argument('-e', '--envoy', action='store', default='envoy', help='Address of Enphase Envoy.')
    parser.add_argument('-t', '--min-request-interval', action='store', default=10, type=int, help='Minimum time between requests to Envoy.')
    parser.add_argument('-p', '--port', action='store', default=9101, type=int, help='Port number for prometheus to scrape from.')
    parser.add_argument('-d', '--debug', action='store_true', help='Debug output (implies verbose, above)')
    parser.add_argument('--version', action='version')
    args = parser.parse_args()

    loglevel = logging.INFO
    if args.debug:
        loglevel = logging.DEBUG
    logging.basicConfig(level=loglevel)

    envoy_address = args.envoy
    prometheus_port = args.port
    logging.info('Prometheus exporter for Enphase Envoy starting up. Reading data from %s, publishing on port %d.' %
                 (envoy_address, prometheus_port))

    exporter_server = ThreadingHTTPServer(('', prometheus_port), ExporterRequestHandler)

    logging.debug('Starting server...')
    exporter_server.serve_forever()
    logging.debug('Server finished.')


if __name__ == '__main__':
    start_exporter()

