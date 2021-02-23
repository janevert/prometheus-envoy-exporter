FROM python:3.9-alpine

WORKDIR /usr/local/bin

RUN mkdir -p /usr/local/bin
COPY envoy_exporter.py /usr/local/bin/envoy_exporter.py

EXPOSE 9101

ENTRYPOINT [ 'python', '/usr/local/bin/envoy_exporter.py' ]
