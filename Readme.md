This project is to extract information from my home solar system. It uses Enphase IQ
micro inverters and an Envoy to control and monitor it.

I intend to license this as some form of GPL3, but need to think about this.
This needs to be settled before pushing to Github.

I intend to run this in docker, so there should be a Dockerfile and instructions about that too.
However, there is no issue running this directly on a machine.

I intend to have the root page return some static information (about inverters, etc), have a development
page where the last inventory and production json is available, and possible the last ones from midnight and midday.
Also a list other development support information (unknown statuses, for example).

Configuration
=============
The envoy hostname is needed. The default is "envoy", which is what it does in my home.

Also the port for prometheus metrics is needed. The default is 9101, the next after the prometheus node exporter.

The minimum time between requests to the envoy is by default 10s.

Use the -d flag to get more diagnostics.

Docker
======

See the included Dockerfile and docker-compose.yml for examples how it can be used with Docker.

When using Docker, the output of the script is available from 'docker logs' and 'docker-compose logs'.
