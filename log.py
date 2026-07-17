import os
from loguru import logger
from slack_sdk.errors import SlackApiError

#remove default console log
logger.remove()

logger.add("logs/btsbot.log",rotation="1 day",retention="14 days",level="DEBUG")

# convenience assignments into namespace for ease/brevity when calling from other modules
trace = logger.trace
debug = logger.debug
info = logger.info
success = logger.success
warning = logger.warning
error = logger.error
critical = logger.critical

# assignments so code that uses these loggers doesn't break if called before loggers are properly set up
slack = logger
slack_sink = None
slack_ts = None
music_team = logger
music_team_sink = None
music_team_ts = None

def generate_slack_sink(client, channel, log_handle=None, level=None):
    """
        create and attach a sink for posting to a slack channel.
        If no team is specified, it will print all messages for any slack sink
            (use this for the generic logs channel)
    """
    if log_handle is None:
        log_handle = "slack"
        location_string = "slack"
        log_format = "{level: <8} | {name}:{function}:{line} - {message}"
        text_in_code_block = True
        reply_in_thread = False
    else:
        location_string = f"slack_{log_handle}"
        log_format = "{message}"
        text_in_code_block = False
        reply_in_thread = True
    if level is None:
        level = "INFO"

    def slack_sink(message):
        """
        Custom sink function that Loguru passes log messages to.
        The 'message' argument contains the pre-formatted string.
        """
        if reply_in_thread:
            ts = globals()[f"{log_handle}_ts"]
            if ts is None:
                try:
                    # Send a parent message for replying in thread
                    msg_sent = client.chat_postMessage(
                        channel=channel,
                        text=f"This message is a place for {log_handle} bot logs in thread."
                    )
                    ts = msg_sent['ts']
                    globals()[f"{log_handle}_ts"] = ts
                    import data_models
                    data_models.set_persistent_value(f"{log_handle}_ts",ts)
                except SlackApiError as e:
                    # Fallback to stderr if the Slack API call fails
                    print(f"Failed sending log to Slack: {e.response['error']}")
        else:
            ts = None

        if text_in_code_block:
            text = f"```{message}```"  # Wrap in markdown code block for clean formatting
        else:
            text = f"{message}"  # Plain message for human channels
        try:
            # Send the log message text safely to your Slack channel
            client.chat_postMessage(
                channel=channel,
                thread_ts = ts,
                #text=f"```{message}```"  # Wrap in markdown code block for clean formatting
                text=text
            )
        except SlackApiError as e:
            # Fallback to stderr if the Slack API call fails
            print(f"Failed sending log to Slack: {e.response['error']}")

    #TODO remove old hander first
    handle_sink = logger.add(
        slack_sink,
        level=level,
        format=log_format,
        filter=lambda record: "location" in record["extra"] and record["extra"].get("location").startswith(location_string)
    )

    handle_logger = logger.bind(location=location_string)
    # save sink and logger to module globals for later use
    #  - (may need sink later if we change log level)
    globals()[log_handle] = handle_logger
    globals()[f"{log_handle}_sink"] = handle_sink

