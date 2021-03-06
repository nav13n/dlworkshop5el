#!/usr/bin/env bash
apt-get update
apt-get install -y python-dev
apt-get install -y python-numpy
apt-get install -y cython
apt-get install -y python-scipy
apt-get install -y python-matplotlib
apt-get install -y python-sklearn
apt-get install -y python-setuptools
if ! [ -L /var/www ]; then
  rm -rf /var/www
  ln -fs /vagrant /var/www
fi
