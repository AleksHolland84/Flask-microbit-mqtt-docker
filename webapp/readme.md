
# send to register microbit

client.publish("microbits/register/mb1", "")

# send every minute or on startup
client.publish("microbits/mb1/heartbeat", "online")



