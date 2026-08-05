FROM odoo:19.0

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-pip \
    && python3 -m pip install \
        --no-cache-dir \
        --break-system-packages \
        "httpx[http2]" \
        "PyJWT" \
    && python3 -c "import sys, httpx, h2, jwt; print('DEPENDENCIES_OK'); print(sys.executable); print(httpx.__file__)" \
    && rm -rf /var/lib/apt/lists/*

USER odoo