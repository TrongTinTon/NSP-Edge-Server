FROM odoo:19

USER root

COPY ./extra-addons/nsp_zeroconfig/requirements.txt /tmp/nsp_zeroconfig_requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/nsp_zeroconfig_requirements.txt

USER odoo
