#!/usr/bin/ruby
# Small TCP relay for macOS with Colima: the container cannot always reach cameras on the LAN,
# but it can reach the Mac. Point Pies Watch at RELAY_BIND:RELAY_PORT instead of the camera.
require "socket"

bind_address = ENV.fetch("RELAY_BIND")            # e.g. the Mac's LAN address
bind_port = Integer(ENV.fetch("RELAY_PORT", "15554"))
upstream_host = ENV.fetch("RELAY_UPSTREAM")       # the camera or recorder address
upstream_port = Integer(ENV.fetch("RELAY_UPSTREAM_PORT", "554"))

server = TCPServer.new(bind_address, bind_port)
warn "Pies Watch RTSP relay listening on #{bind_address}:#{bind_port}"
loop do
  client = server.accept
  Thread.new(client) do |downstream|
    upstream = nil
    begin
      upstream = TCPSocket.new(upstream_host, upstream_port)
      pipes = [
        Thread.new { IO.copy_stream(downstream, upstream); upstream.close_write rescue nil },
        Thread.new { IO.copy_stream(upstream, downstream); downstream.close_write rescue nil },
      ]
      pipes.each(&:join)
    rescue IOError, SystemCallError
      nil
    ensure
      downstream.close rescue nil
      upstream.close rescue nil if upstream
    end
  end
end
