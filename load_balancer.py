# Author: Marcos Canales Mayo
# Email: mcanalesmayo@gmail.com
# Description: basic controller module to balance the load of a single switch

from pox.core import core
import pox.lib.packet as libpacket
from pox.lib.packet.ethernet import ethernet
from pox.lib.packet.arp import arp
from pox.lib.addresses import IPAddr, EthAddr
import pox.openflow.libopenflow_01 as of
from pox.openflow.of_json import *
import random
import threading

log = core.getLogger()

"""
Flows timeout, in seconds
"""
FLOW_TIMEOUT = 10

"""
Scheduling methods (used to choose a server to handle the service request)
"""
SCHED_RANDOM = 0
SCHED_ROUNDROBIN = 1
SCHED_METHOD = SCHED_RANDOM

"""
Statistics request period, in seconds
"""
STATS_REQ_PERIOD = 3

class Host (object):
	def __init__ (self, mac, ip, port):
		self.mac = mac
		self.ip = ip
		self.port = port
		self.req_n = 0

	def __str__ (self):
		return "MAC: " + str(self.mac) + " | IP: " + str(self.ip) + " | Port:" + str(self.port)

"""
Net params
"""
MAC_PREFIX = "00:00:00:00:00"
NET_PREFIX = "10.0.0"

"""
Switch params
"""
SWITCH_IP_SUFFIX = "13"
SWITCH_MAC_SUFFIX = "13"
SWITCH_HOST = Host(EthAddr(MAC_PREFIX + ":" + SWITCH_MAC_SUFFIX), IPAddr(NET_PREFIX + "." + SWITCH_IP_SUFFIX), None)

"""
Creates a list of hosts from 'start' to 'end'
"""
def fill_hosts_list (start, end):
	L = {}
	i = 0
	for host in range(start, end + 1):
		if host < 10:
			mac_suffix = "0" + str(host)
		else:
			mac_suffix = str(host)

		L[i] = Host(EthAddr(MAC_PREFIX + ":" + mac_suffix), IPAddr(NET_PREFIX + "." + str(host)), host)
		i += 1
	return L

"""
List of hosts
"""
CL_START = 1
CL_END = 6
SV_START = 7
SV_END = 12

CL_HOSTS = fill_hosts_list(CL_START, CL_END)
SV_HOSTS = fill_hosts_list(SV_START, SV_END)

"""
Gets a host by its mac
"""
def get_host_by_mac (hosts_list, mac):
	return next( (x for x in hosts_list.values() if str(x.mac) == str(mac)), None)

"""
Gets a host by its ip
"""
def get_host_by_ip (hosts_list, ip):
	return next( (x for x in hosts_list.values() if str(x.ip) == str(ip)), None)

class stats_req_thread (threading.Thread):
	def __init__ (self, connection, stop_flag):
		threading.Thread.__init__(self)

		self.connection = connection
		self.stop_flag = stop_flag

	"""
	Periodically asks the statistics to the switch, till stop flag is raised
	"""
	def run (self):
		while not self.stop_flag.wait(STATS_REQ_PERIOD):
			msg = of.ofp_stats_request()
			msg.type = of.OFPST_PORT
			msg.body = of.ofp_port_stats_request()
			self.connection.send(msg)

class proxy_load_balancer (object):
	def __init__ (self, connection):
		self.connection = connection

		# Timer should be global in order to be stopped when ConnectionDown event is raised
		global stop_flag
		stop_flag = threading.Event()
		stats_req_thread(self.connection, stop_flag).start()

		# If RR is the scheduling method, then choose a random server to start it
		if SCHED_METHOD is SCHED_ROUNDROBIN:
			self.last_server_idx = random.randint(0, len(SV_HOSTS))

		# Listen to the connection
		connection.addListeners(self)

	# AggregateFlowStats tables are deleted everytime a FlowMod is sent
	# Alternative is PortStats
	def _handle_PortStatsReceived (self, event):
		log.info("Stats received: %s" % (str(flow_stats_to_list(event.stats))))

	def _handle_PacketIn (self, event):
		frame = event.parse()

		# ARP request
		if frame.type == frame.ARP_TYPE:
			log.debug("Handling ARP Request from %s" % (frame.next.protosrc))
			self.arp_handler(frame, event)
		# Service request
		elif frame.type == frame.IP_TYPE:
			log.debug("Handling Service request from %s" % (frame.next.srcip))
			self.service_handler(frame, event)

	"""
	An ARP reply with switch fake MAC has to be sent
	"""
	def arp_handler (self, frame, event):
		"""
		Builds an Ethernet frame with the switch fake MAC
		"""
		def build_eth_frame (frame):
			eth_reply_msg = ethernet()
			eth_reply_msg.type = ethernet.ARP_TYPE
			eth_reply_msg.dst = frame.src
			# Switch fake MAC
			eth_reply_msg.src = SWITCH_HOST.mac
			return eth_reply_msg

		"""
		Builds an ARP reply packet with the switch fake MAC
		and the transparent proxy IP
		"""
		def build_arp_reply (frame, arp_request_msg, is_client):
			arp_reply_msg = arp()
			arp_reply_msg.opcode = arp.REPLY
			# Switch fake MAC
			arp_reply_msg.hwsrc = SWITCH_HOST.mac
			arp_reply_msg.hwdst = arp_request_msg.hwsrc
			# Transparent proxy IP if is client
			if is_client == True:
				arp_reply_msg.protosrc = SWITCH_HOST.ip
			# Client IP if is server
			else:
				arp_reply_msg.protosrc = arp_request_msg.protodst
			arp_reply_msg.protodst = arp_request_msg.protosrc
			return arp_reply_msg

		# Check whether ARP Request is coming from a client or a server
		# If list has elements then True, else False
		is_client = False if get_host_by_mac(CL_HOSTS, frame.src) is None else True

		# Build Ethernet frame and ARP packet
		eth_reply_msg = build_eth_frame(frame)
		# ARP Request is the payload of the Ethernet frame
		arp_request_msg = frame.next
		arp_reply_msg = build_arp_reply(frame, arp_request_msg, is_client)

		# Encapsulate
		eth_reply_msg.set_payload(arp_reply_msg)

		# Send OF msg to output ARP packet
		msg = of.ofp_packet_out()
		msg.data = eth_reply_msg.pack()
		msg.actions.append(of.ofp_action_output(port = of.OFPP_IN_PORT))
		msg.in_port = event.port
		log.debug("Sending OFP ARP Packet Out" % ())
		self.connection.send(msg)

	"""
	Service packets should be balanced between all servers of the pool
	"""
	def service_handler (self, frame, event):
		"""
		Chooses the next server according to the scheduling method
		"""
		def choose_server ():
			# Random choice
			if SCHED_METHOD is SCHED_RANDOM:
				chosen_server = random.choice(SV_HOSTS)
			# Round Robin choice
			elif SCHED_METHOD is SCHED_ROUNDROBIN:
				self.last_server_idx = (self.last_server_idx + 1) % len(SV_HOSTS)
				chosen_server = SV_HOSTS[self.last_server_idx]
			return chosen_server

		"""
		Checks whether the frame contains an ICMP Reply packet
		"""
		def is_icmp_reply (frame):
			if get_host_by_mac(SV_HOSTS, frame.src) is None:
				return False
			return True

		"""
		Builds the ICMP reply from the Switch to the Client
		"""
		def build_icmp_reply (frame, dst_host):
			# Same actions that FlowMod would do:
			# - Update the src IP and MAC to the SWITCH MAC and IP
			# - Update the dst MAC to the client one (incoming frames from servers also have MAC dst = Switch fake MAC)
			frame.src = SWITCH_HOST.mac
			frame.dst = dst_host.mac
			frame.next.srcip = SWITCH_HOST.ip
			return frame

		packet = frame.next

		# Check whether it is a ICMP packet reply (PacketIn triggered because the flow rule timed out)
		if is_icmp_reply(frame) == True:
			# Send OF msg to output ICMP reply packet
			msg = of.ofp_packet_out()
			dst_host = get_host_by_ip(CL_HOSTS, packet.dstip)
			msg.data = build_icmp_reply(frame, dst_host).pack()
			msg.actions.append(of.ofp_action_output(port = dst_host.port))
			msg.in_port = event.port
			log.debug("Sending OFP ICMP Reply Packet Out" % ())
			self.connection.send(msg)
			return None

		# Choose server
		chosen_server = choose_server()
		chosen_server.req_n += 1
		
		# The path must be set from end to start of frame direction, i.e. firstly the server-to-client
		# and then client-to-server, in order to avoid the frame being received by the server
		# and then sent to the client before the switch has updated all the flow rules

		# Server -> Client path
		msg = of.ofp_flow_mod()
		msg.idle_timeout = FLOW_TIMEOUT
		msg.hard_timeout = FLOW_TIMEOUT
		# Packets coming from the chosen server
		msg.match.in_port = chosen_server.port
		# Rule only for IP packets (service)
		msg.match.dl_type = ethernet.IP_TYPE
		# Ethernet src address matching the MAC of the chosen server
		msg.match.dl_src = chosen_server.mac
		# Ethernet dst address matching the Switch fake MAC
		msg.match.dl_dst = SWITCH_HOST.mac
		# Network src address matching the IP of the chosen server
		msg.match.nw_src = chosen_server.ip
		# Network dst address matching the IP of the client
		msg.match.nw_dst = packet.srcip

		log.debug("Chosen server for %s is %s" % (packet.srcip, chosen_server.ip))
		# If matches then, in order to behave as a transparent proxy:
		# - Update the src IP and MAC to the SWITCH MAC and IP
		# - Update the dst MAC to the client one (incoming frames from servers also have MAC dst = Switch fake MAC)
		msg.actions.append(of.ofp_action_dl_addr.set_src(SWITCH_HOST.mac))
		msg.actions.append(of.ofp_action_dl_addr.set_dst(frame.src))
		msg.actions.append(of.ofp_action_nw_addr.set_src(SWITCH_HOST.ip))
		# - Forward to the client
		msg.actions.append(of.ofp_action_output(port = event.port))
		# Send OF msg to update flow rules
		log.debug("Sending OFP FlowMod Server -> Client path" % ())
		self.connection.send(msg)

		# Client -> Server path
		msg = of.ofp_flow_mod()
		msg.idle_timeout = FLOW_TIMEOUT
		msg.hard_timeout = FLOW_TIMEOUT
		msg.data = event.ofp
		# Packets coming from the client
		msg.match.in_port = event.port
		# Rule only for IP packets (service)
		msg.match.dl_type = ethernet.IP_TYPE
		# Ethernet src address matching the MAC of the client
		msg.match.dl_src = frame.src
		# Ethernet dst address matching the MAC of the proxy
		msg.match.dl_dst = SWITCH_HOST.mac
		# Network src address matching the IP of the client
		msg.match.nw_src = packet.srcip
		# Network dst address matching the IP of the proxy
		msg.match.nw_dst = SWITCH_HOST.ip

		# If matches then:
		# - Update the src IP and MAC to the chosen server
		msg.actions.append(of.ofp_action_dl_addr.set_dst(chosen_server.mac))
		msg.actions.append(of.ofp_action_nw_addr.set_dst(chosen_server.ip))
		# - Forward to the chosen server
		msg.actions.append(of.ofp_action_output(port = chosen_server.port))
		# Send OF msg to update flow rules
		log.debug("Sending OFP FlowMod Client -> Server path" % ())
		self.connection.send(msg)

"""
Controller
"""
class load_balancer (object):
	def __init__ (self):
		# Add listeners
		core.openflow.addListeners(self)

	"""
	New connection from switch
	"""
	def _handle_ConnectionUp (self, event):
		log.debug("Switch connected" % ())
		# Create load balancer
		proxy_load_balancer(event.connection)

	"""
	Connection from switch closed
	"""
	def _handle_ConnectionDown (self, event):
		log.debug("Switch disconnected" % ())
		# Stop stats req timer
		stop_flag.set()

def launch ():
	core.registerNew(load_balancer)
