FROM odoo:19.0

USER root

COPY ./extra-addons/nsp_zeroconf/requirements.txt \
     /tmp/nsp_zeroconf-requirements.txt

RUN python3 -m pip install \
    --no-cache-dir \
    --break-system-packages \
    -r /tmp/nsp_zeroconf-requirements.txt \
    && rm -f /tmp/nsp_zeroconf-requirements.txt

USER odoo