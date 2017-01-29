# sdn_load_balancer

## Description

SDN Load Balancer. Example scheme with 6 clients <-> Switch (Transparent proxy, load balancer) <-> Pool of 6 servers.

The controller application is connected to the switch in order to modify flow rules and balance the load among all servers. Clients aren't aware of backend servers, they only know about the transparent proxy (switch).

## Author

[Marcos Canales Mayo](https://github.com/MarcosCM)
