FROM python:3.9-alpine

RUN pip install --no-cache-dir requests==2.25.1
RUN pip install --no-cache-dir jinja2==2.11.3
RUN pip install --no-cache-dir prometheus-client==0.9.0

RUN mkdir -p /usr/local/bin
COPY envoy_exporter.py /usr/local/bin/envoy_exporter.py

EXPOSE 9101

ENTRYPOINT [ "python", "/usr/local/bin/envoy_exporter.py" ]
