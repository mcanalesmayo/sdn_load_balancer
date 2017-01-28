sudo mn -c
sudo fuser -k 6633/tcp
sudo killall controller
python pox.py log.level --DEBUG load\_balancer