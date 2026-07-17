read -r pid <$XDG_RUNTIME_DIR/btsbot/btsbot.pid
echo $pid
kill -SIGUSR1 $pid


