# sdn_load_balancer

## Description

SDN Load Balancer. Example scheme with 6 clients <-> Switch (Transparent proxy, load balancer) <-> Pool of 6 servers.

The controller application (POX, Python) is connected to the switch in order to modify flow rules and balance the load among all servers. Clients aren't aware of backend servers, they only know about the transparent proxy (switch).

To run the SDN LB run the script ``start_lb.sh``.
