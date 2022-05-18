#!/usr/bin/bash

cd ~/hoodie
sudo cp teletubby.service /lib/systemd/system/
sudo chmod 644 /lib/systemd/system/teletubby.service
sudo systemctl daemon-reload
sudo systemctl enable teletubby.service