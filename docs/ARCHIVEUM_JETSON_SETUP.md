# Archiveum Jetson Setup

## Quick start

```bash
cd ~/Archiveum
chmod +x install_archiveum.sh
./install_archiveum.sh
```

The installer will:

- create the virtual environment
- install Python dependencies
- patch `archiveum_settings.json`
- optionally enable voice mode
- run the self-test
- optionally install the systemd service

## Manual service install

```bash
sudo cp /home/<user>/Archiveum/deploy/archiveum.service /etc/systemd/system/archiveum.service
sudo systemctl daemon-reload
sudo systemctl enable archiveum.service
sudo systemctl start archiveum.service
sudo systemctl status archiveum.service
```

Use the installer when possible, because it renders the service template with the correct Linux username and project path.
