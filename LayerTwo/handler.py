import json
import pickle
import random
import secrets
import time
from typing import Optional

from readerwriterlock import rwlock

from LayerOne.network.server.custom.handler import Handler
from LayerOne.network.server.custom.client import Client
from LayerOne.network.extra.chat_formatting import Color, Style, RESET
from LayerOne.network.packet import Packet
from LayerOne.network.server.custom.server import Server
from LayerOne.types.common import ProtocolException
from LayerOne.types.native import UShort, Int, UByte, Byte, Boolean, Double, Float
from LayerOne.types.string import String
from LayerOne.types.varint import VarInt

from network.extra.world_manipulation import world_to_map_chunk_bulk_datas

class L2Handler (Handler):
    def __init__ (self, client: Client, is_first: bool):
        self.client: Client = client
        self.server: Server = client.server

        print (f"{self.client.address} connected, is first: {is_first}")

        if is_first:
            with open (f"../../LayerTwo/worlds/{self.server.initial_storage ['base_world_name']}.l1w", "rb") as base_world_file:
                self.server.runtime_storage ["base_world"] = pickle.load (base_world_file)
            print (len (self.server.runtime_storage ["base_world"] ["chunk_columns"]))

        self.state_lock = rwlock.RWLockFair ()
        self.state = 0

        self.protocol_version = None

        self.uuid = None
        self.username = None

        self.last_keep_alive_at = 0
    def ext__in_play (self):
        with self.state_lock.gen_rlock (): return self.state == 3
    def packet_received (self, packet_id: int, packet_data: bytes) -> Optional [bool]:
        def unknown_packet (state_name: str): raise ProtocolException (f"Unknown packet {packet_id} in state {state_name}")
        state_rlock = self.state_lock.gen_rlock ()
        state_rlock.acquire ()
        if self.state == 0: # Handshaking
            state_rlock.release ()
            if packet_id == 0x00: # Handshake
                handshake = Packet.decode_fields (packet_data, (VarInt, String, UShort, VarInt))
                self.protocol_version = handshake [0]
                next_state = handshake [3]
                print (f"Switching to state {next_state}")
                with self.state_lock.gen_wlock (): self.state = next_state
            else: unknown_packet ("Handshaking")
        elif self.state == 1: # Status
            state_rlock.release ()
            if packet_id == 0x00: # Request
                clients_in_play = 0
                with self.server.clients_lock:
                    for client in self.server.clients:
                        handler: L2Handler = client.handler
                        if handler.ext__in_play (): clients_in_play += 1
                response_json = {
                    "version": {
                        "name": f"LayerTwo {self.server.initial_storage ['implements_protocol_version'] ['name']}",
                        "protocol": self.server.initial_storage ["implements_protocol_version"] ["number"]
                    },
                    "players": {
                        "online": clients_in_play,
                        "max": self.server.initial_storage ["max_players"],
                        "sample": []
                    },
                    "description": {
                        "text": self.server.initial_storage ["motd"]
                    }
                }
                response_data = Packet.encode_fields ((json.dumps (response_json), String))
                self.client.send (0x00, response_data) # Response
                print ("Sent response to status request")
            elif packet_id == 0x01: # Ping
                self.client.send (0x01, packet_data) # Pong
                print ("Sent pong to status ping")
            else: unknown_packet ("Status")
        elif self.state == 2: # Login
            state_rlock.release ()
            def send_disconnect (reason: str):
                self.client.send (0x00, Packet.encode_fields ((json.dumps ({"text": reason}), String)))
            if self.protocol_version != self.server.initial_storage ["implements_protocol_version"] ['number']:
                send_disconnect (f"Your client is on protocol version {self.protocol_version} while LayerTwo needs protocol version "
                                 f"{self.server.initial_storage ['implements_protocol_version'] ['number']} ({self.server.initial_storage ['implements_protocol_version'] ['name']})!")
                return True
            self.uuid = "xxxxxxxx-xxxx-3xxx-xxxx-xxxxxxxxxxxx"
            while 'x' in self.uuid:
                self.uuid = self.uuid.replace ('x', random.choice ("0123456789abcdef"))
            self.username = Packet.decode_fields (packet_data, (String,)) [0]

            self.client.enable_compression (compression_threshold = self.server.initial_storage ["compression_threshold"])

            self.client.send (0x02, Packet.encode_fields ((self.uuid, String), (self.username, String))) # Login Success
            with self.state_lock.gen_wlock (): self.state = 3

            self.client.send (0x01, Packet.encode_fields ((0, Int), (1, UByte), (self.server.runtime_storage ["base_world"] ["dimension"], Byte), (0, UByte), (self.server.initial_storage ["max_players"], UByte), ("default", String), (False, Boolean))) # Join Game

            self.client.send (0x08, Packet.encode_fields ((0, Double), (70, Double), (0, Double), (0, Float), (0, Float), (0x00, UByte))) # Player Position and Look

            print ("PREPARING MAP CHUNK BULK")
            map_chunk_bulk_datas = world_to_map_chunk_bulk_datas (self.server.runtime_storage ["base_world"])
            print (len (map_chunk_bulk_datas))
            print ("PREPARED MAP CHUNK BULK")
            for map_chunk_bulk_data in map_chunk_bulk_datas:
                self.client.send (0x26, map_chunk_bulk_data)
        elif self.state == 3: # Play
            state_rlock.release ()
            now = time.time ()
            ten_seconds_ago = now - 10
            if self.last_keep_alive_at < ten_seconds_ago:
                self.client.send (0x00, Packet.encode_fields ((int.from_bytes (secrets.token_bytes (4), byteorder = "big", signed = True), VarInt))) # Keep Alive
                print ("sent keep alive")
                self.last_keep_alive_at = now
            if packet_id == 0x00: # Keep Alive
                print ("ignoring keep alive response")
            elif packet_id == 0x01: # Chat Message
                print (f"received chat message {Packet.decode_fields (packet_data, (String,)) [0]}")
            elif packet_id == 0x03: # Player
                on_ground = Packet.decode_fields (packet_data, (Boolean,)) [0]
                print (f"received new player basic state; on ground: {on_ground}")
            elif packet_id == 0x04: # Player Position
                x, feet_y, z, on_ground = Packet.decode_fields (packet_data, (Double, Double, Double, Boolean))
                print (f"received new player position: {x},{feet_y},{z}; on ground: {on_ground}")
            elif packet_id == 0x05: # Player Look
                yaw, pitch, on_ground = Packet.decode_fields (packet_data, (Float, Float, Boolean))
                print (f"received new player look: {yaw},{pitch}; on ground: {on_ground}")
            elif packet_id == 0x06: # Player Look
                x, feet_y, z, yaw, pitch, on_ground = Packet.decode_fields (packet_data, (Double, Double, Double, Float, Float, Boolean))
                print (f"received new player look: {yaw},{pitch}; on ground: {on_ground}")
            else: print ("some random play packet, no idea")
        else:
            state_rlock.release ()
            raise ProtocolException (f"Unknown state with ID {self.state}")
        return False # don't disconnect
    def disconnected (self, initiated_by_server: bool): pass

if __name__ == "__main__":
    Server (quiet = True, host = ("0.0.0.0", 25569), initial_storage = {
        "motd": f"{Style.BOLD}{Color.WHITE}LayerTwo (beta){RESET}{Style.ITALIC}{Color.GRAY} by An0nDev",
        "max_players": 8,
        "implements_protocol_version": {
            "number": 47,
            "name": "1.8.9"
        },
        "compression_threshold": 256,
        "base_world_name": "oceantest"
    }, handler_class = L2Handler)