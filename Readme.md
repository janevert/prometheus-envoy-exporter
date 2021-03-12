This project is to extract information from my home solar system to prometheus. It uses Enphase IQ
micro inverters and an Envoy to control and monitor it.

This project is licensed under GNU GPLv3. See License.txt for the full license text.

Configuration
=============
There are command line options to change these configuration items:

The Envoy hostname is needed. The default is "envoy", which is the default of the Envoy device.

Also the port for prometheus metrics is needed. The default is 9101, the next after the prometheus node exporter.

The minimum time between requests to the envoy is by default 10s.

Use the -d flag to get more diagnostics. Useful for developers.

Docker
======

See the included Dockerfile and docker-compose.yml for examples how it can be used with Docker.

When using Docker, the output of the script is available from 'docker logs' and 'docker-compose logs'.

