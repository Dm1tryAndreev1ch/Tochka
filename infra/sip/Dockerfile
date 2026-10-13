FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55 AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl build-essential pkg-config libssl-dev libxml2-dev libsqlite3-dev \
    uuid-dev libedit-dev libncurses-dev libcurl4-openssl-dev \
    && rm -rf /var/lib/apt/lists/*
ARG ASTERISK_VERSION=22.11.0
ARG ASTERISK_SHA256=3bd5ee040509a3d3cd9b1ba9520c18e6ec0a7e7981ca68c457dcd36ba3c54d94
RUN curl --fail --location --retry 3 "https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-${ASTERISK_VERSION}.tar.gz" -o /tmp/asterisk.tar.gz \
    && printf '%s  /tmp/asterisk.tar.gz\n' "$ASTERISK_SHA256" | sha256sum -c - \
    && mkdir /tmp/asterisk && tar -xzf /tmp/asterisk.tar.gz -C /tmp/asterisk --strip-components=1 \
    && cd /tmp/asterisk \
    && ./configure --prefix=/usr --libdir=/usr/lib --sysconfdir=/etc --with-pjproject-bundled --with-jansson-bundled \
    && make menuselect.makeopts \
    && menuselect/menuselect --disable BUILD_NATIVE --disable chan_iax2 --enable app_audiosocket --enable chan_audiosocket menuselect.makeopts \
    && make -j2 \
    && make DESTDIR=/opt/asterisk install

FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libssl3t64 libxml2 libsqlite3-0 libuuid1 libedit2 libncurses6 libcurl4t64 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/asterisk/usr/ /usr/
COPY --from=build /opt/asterisk/var/lib/asterisk/ /usr/share/asterisk/
CMD ["asterisk", "-f"]
