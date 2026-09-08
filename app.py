import os
import re
import signal
from pathlib import Path

import schedule
from schedule import every

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import log
from log import logger

import data_models
from data_models import users,parts,songparts,songs,tags,quals
from data_models import songs_by_id, parts_by_id, users_by_id, slack_name_from_db_name
from data_models import slack_id_from_slack_name, user_id_from_slack_id, create_qual
from data_models import create_tag, rename_tag

import utility
from utility import select_quals,authorized,user_from_slack_id

import commands

app = App(token=os.environ.get("SLACK_BOT_TOKEN"))


@app.event("app_mention")
def handle_app_mention(ack, say, body, client):
    ack()
    return commands.dispatch(body,client,say=say)

@app.event("message")
def handle_message_events(say, body, logger, client):
    log.trace("got message event")
    if 'channel_type' in body['event'] and body['event']['channel_type'] == 'im':
        log.debug("dispatching command from private message to bot")
        return commands.dispatch(body,client,say=say)

@app.action("output-list-button")
def handle_output_list_button(ack, body, say, client):
    ack()
    return commands.dispatch(body,client,say=say)

@app.action("auth_checkbox-action")
def handle_auth_checkbox_action(ack, body, say, client):
    # don't do anything, as we'll handle it on submit
    ack()

@app.action("tag_checkbox-action")
def handle_tag_checkbox_action(ack, body, say, client):
    # don't do anything, as we'll handle it on submit
    ack()

@app.action("overflow-action")
def handle_overflow_action(ack, body, say, client):
    ack()
    log.debug("dispatching command from overflow menu.")
    return commands.dispatch(body,client,say=say)

@app.view("pre interact")
def handle_pre_interact_submit(ack, body, say, respond, client):
    ack()
    log.debug("dispatching command from pre interact submit.")
    return commands.dispatch(body,client,say=say)

view_submit_re = re.compile("""^(
        manage_song_edit |
        admin_user_edit |
        setlist_item_edit |
        manage_gig |
        manage_gig_add_song
        )$""", re.VERBOSE)

#TODO fold in the special cases to generic handler

@app.view(view_submit_re)
def handle_generic_submit(ack, body, say, respond, client):
    errors = commands.handle_submit(body,client,say=say)
    # errors should be a dict with block_id keys and err_msg text values
    #  to get slack to show messages
    if errors:
        ack(response_action="errors",errors=errors)
    else:
        ack()

@app.view("manage tag create_new")
def handle_create_new_tag_submit(ack, body, say, respond, client):
    ack()
    view_id = body['view']['external_id']
    create_tag(body['view']['state']['values']['tag_name']['create_new_tag-action']['value'])
    commands.update_tag_manage_view(body,client,view_id=view_id)

@app.view("manage tag rename")
def handle_rename_tag_submit(ack, body, say, respond, client):
    ack()
    #print(body['view']['title'])
    tag_id = int(body['view']['title']['text'].split()[-1])
    view_id = body['view']['external_id']
    rename_tag(tag_id,body['view']['state']['values']['tag_name']['rename_tag-action']['value'])
    commands.update_tag_manage_view(body,client,view_id=view_id)


## scheduled actions

@schedule.repeat(every().hour.at(":00"))
#@schedule.repeat(every().minute.at(":00"))
def schedule_log_heartbeat():
    log.slack.info("I am alive")
    #for job in schedule.get_jobs():
        #log.debug(f"{job} {job.at_time}")
        #log.debug(f"{dir(job)}")


######## old/testing stuff

@app.command("/pre")
def handle_pre_command(ack, respond, command, client):
    ack()
    hints = command['text'].split()


@app.message("gig-songs")
def message_gig_songs(message, say):
    for key in message.keys():
        print(f"{key}:  {message[key]}")
    lines = []

    words = message['text'].split()
    ids = [user_from_slack_id(x) for x in words[1:]]
    id_quals = select_quals(quals,user_ids=ids,only_most_recent=True)
    display_non_performable = True
    #hack for getting proper order for parts
    parts = ["Tenor","Lead","Bari","Bass"]

    if len(ids) > 0:
        sorted_songs = sorted([(utility.qual_strength(id_quals,s),s) for s in songs],reverse=True)
        performable = True
        for ((qual_strength,missing_parts),song) in sorted_songs:
            if performable and qual_strength < 1:
                if not display_non_performable:
                    break
                # insert separator to show songs above are performable and songs below not
                lines.append("\n\n ------ Non-performable ------\n\n")
                performable = False
            qual_strs = []
            song_quals = utility.select_quals(id_quals,song_id=song.id,only_most_recent=True)
            for part_name in parts:
                part = [p for p in song.parts if p.part.name == part_name][0]
                part_quals = utility.select_quals(song_quals, part_id=part.part_id)
                if len(ids) > 0:
                    names = set([users_by_id[q.user_id].name for q in part_quals])
                    if len(names) >0:
                        names_str = '/'.join(names)
                    else:
                        names_str = 'None'
                    qual_strs.append(f"{part.part.name}: {names_str}")
                else:
                    qual_strs.append(f"{part.part.name}: {len(part_quals)}")
            qual_strs.append(f"{missing_parts}")
            qual_str = " / ".join(qual_strs)
            lines.append(f"{song.name}  {qual_str}")


    say("\n".join(lines),thread_ts=message['ts'])
    profile = app.client.users_profile_get(user=message['user'])
    print(profile)

@app.message("show-users")
def message_show_users(message, say):
    for key in message.keys():
        print(f"{key}:  {message[key]}")
    users_to_skip = ['slackbot','caguayo']
    slack_users = app.client.users_list()['members']
    for user in slack_users:
        if user['deleted'] or user['is_bot'] or user['name'] in users_to_skip:
            continue
        #print(f"{user['id']} {user['profile']['real_name']}  {user['name']}")
        slack_id_from_slack_name[user['profile']['real_name']] = user['id']
    unused_slack_names = [x for x in slack_id_from_slack_name.keys()]
    lines = []
    for user in users:
        if user.name in slack_id_from_slack_name:
            slack_id = slack_id_from_slack_name[user.name]
            user_id_from_slack_id[slack_id] = user.id
            unused_slack_names.remove(user.name)
        elif ( user.name in slack_name_from_db_name and 
               slack_name_from_db_name[user.name] in slack_id_from_slack_name ):
            user_name = slack_name_from_db_name[user.name]
            slack_id = slack_id_from_slack_name[user_name]
            user_id_from_slack_id[slack_id] = user.id
            unused_slack_names.remove(user_name)
        else:
            slack_id = None
        lines.append(f"{user.name}  {user.id}  {slack_id}")
    lines.append(f"unmapped slack users: {unused_slack_names}")
    say("\n".join(lines),thread_ts=message['ts'])
    #profile = app.client.users_profile_get(user=message['user'])
    #print(profile)

@app.message("show-songs")
def message_show_songs(message, say):
    for key in message.keys():
        print(f"{key}:  {message[key]}")
    words = message['text'].split()[1:]
    lines = [f"hints: {words}"]
    for relevance,song in sorted_songs(words):
        lines.append(f"{relevance} - {song.name}: {song.id}")
    say("\n".join(lines),thread_ts=message['ts'])



@app.action("song_selected")
def song_selected_action(ack, body, logger):
    ack()
    #logger.info(body)
    #print(body.keys())
    song_name = body['actions'][0]['selected_option']['text']['text']
    song_id = body['actions'][0]['selected_option']['value']
    print(f"Song selected: {song_name}    id: {song_id}")
    #print(body['view'])

@app.action("user_selected")
def user_selected_action(ack, body, logger):
    ack()
    #logger.info(body)
    #print(body.keys())
    #print(body['actions'][0])
    slack_id = body['actions'][0]['selected_user']
    user_id = user_from_slack_id(slack_id)
    user_name = users_by_id[user_id].name
    print(f"User selected: {slack_id} {user_id} {user_name}")

def song_select_menu(hints):
    option_objects = []
    for (relevance,song) in sorted_songs(hints):
        option_objects.append(
            {
                "text": {
                    "type": "plain_text",
                    "text": f"{song.name}"
                    },
                "value": f"{song.id}"
            } 
        )
    return {
        "type": "section",
        "block_id": "songs_select",
        "text": {"type": "mrkdwn", "text": "Which Song?"},
        "accessory": {
            "action_id": "song_selected",
            "type": "static_select",
            "placeholder": {
                "type": "plain_text",
                "text": "Select a song"
            },
            "options": option_objects
        }
    }

def sorted_songs(hints):
    '''
        output a list of sorted tuples, each containing (relevance,song)
        relevance is based on the 'hints' list.  Hints can be words in the title,
            song tags, (in the future, other metadata songs may have)
    '''
    lower_hints = [ x.lower() for x in hints ]
    if hints:
        hint_count = float(len(hints))
    else:
        hint_count = 1
    tuples = []
    for song in songs:
        relevance = 0
        song_tags = " ".join([tag.name.lower() for tag in song.tags])
        for hint in lower_hints:
            if hint in song.name.lower():
                relevance += 1
            if hint in song_tags:
                relevance += 1
        #print(song.tags)
        tuples.append((relevance/hint_count,song))
    tuples.sort(reverse=True)
    return tuples

def scheduler_heartbeat():
    """ activate the scheduler by calling
            kill -SIGUSR1 <PID>
        For a good balance of performant and not resource-intensive, maybe call from cron
            every 5 minutes
    """
    log.debug(f"scheduler heartbeat: {schedule.idle_seconds()} seconds until next job")
    schedule.run_pending()

def handle_TERM_signal(sig,frame):
    raise KeyboardInterrupt()

def handle_USR1_signal(sig,frame):
    scheduler_heartbeat()

def write_pid_file(pid):
    #PID_PATH = "/var/run/btsbot.pid"
    # Determine the right base path dynamically
    xdg_runtime = os.environ.get('XDG_RUNTIME_DIR')
    if xdg_runtime:
        pid_dir = Path(xdg_runtime) / 'btsbot'
    else:
        pid_dir = Path.home() / '.local' / 'state' / 'btsbot'

    # Ensure the directory exists
    pid_dir.mkdir(parents=True, exist_ok=True)
    pid_file = pid_dir / 'btsbot.pid'

    with open(pid_file,"w") as file:
        file.write(f"{pid}")
    return pid_file

def delete_pid_file(pid_file):
    Path(pid_file).unlink(missing_ok=True)

### TODO items/ feature requests
#   detect other bot connections/disconnect
#   add "deleted" flag to songs and use it for deletion
#   add ability to manage song parts (pitches)
#   add song duration, song written key to song table
### change "tags" nomenclature to something less confusing "categories"?
### congratulate user when qualified
### Song learning priority
### Manage non-slack users
### Database persistence/migration/backup
### calendar integration
#       https://learnpython.com/blog/working-with-icalendar-with-python/
### TODO management: tasks can have userids and channel ids (chan get notified upon completion)

if __name__ == "__main__":
    # make sure killing server causes 'goodbye' message
    signal.signal(signal.SIGTERM, handle_TERM_signal)
    # handle SIGUSR1 to do scheduled tasks
    signal.signal(signal.SIGUSR1, handle_USR1_signal)
    if "SLACK_LOG_CHANNEL_ID" in os.environ and "SUPPRESS_SLACK_LOGS" not in os.environ:
        log.generate_slack_sink(app.client,os.environ['SLACK_LOG_CHANNEL_ID'])
    else:
        log.warning("No 'SLACK_LOG_CHANNEL_ID' found in environment.  Logging to slack disabled.")
    pid = f"{os.getpid()}"
    pid_file = write_pid_file(pid)
    prog_info = f"{os.uname().nodename}:{os.getlogin()}:{pid}"
    log.slack.info(f"BTS-bot connected from {prog_info}")
    #utility.set_admin(os.environ["SLACK_ADMIN_USER"])
    utility.setup_expiry()

    data_models.load_database()
    # now that database is loaded, set music_team loggin thread id, and set up MT logger.
    if "music_team_ts" in data_models.persistent_values:
        log.music_team_ts = data_models.get_persistent_value("music_team_ts")
    if "SLACK_MUSIC_TEAM_CHANNEL_ID" in os.environ:
        log.generate_slack_sink(app.client,os.environ['SLACK_MUSIC_TEAM_CHANNEL_ID'],"music_team")
    else:
        log.warning("No 'SLACK_MUSIC_TEAM_CHANNEL_ID' found in environment.  Logging to music team channel disabled.")
    if len(users) < 1:
        log.slack.warning("empty user database.  Loading from PRE and mapping to slack users.")
        # load database from PRE sheet and slack
        data_models.load_from_PRE('Performance Readiness.xlsx',singers=users,songs=songs,parts=parts,tags=tags)
        data_models.load_database()
        commands.map_users([],{},app.client)
        data_models.auth_from_ENV()


    # One-off commands used for backup or jamboree
    # TODO export PRE spreadsheet and database backup on regular schedule
    #data_models.load_from_form_responses('Quartet Jamboree Attendees (Responses).xlsx')
    #data_models.create_PRE_spreadsheet( 'PRE_backup.xlsx',singers=users,songs=songs,tags=tags,parts=parts,quals=quals)

    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    try:
        handler.start()
    except KeyboardInterrupt:
        log.slack.info(f"BTS-bot shutting down from {prog_info}.  Bye.")
        handler.close()
        delete_pid_file(pid_file)
        raise
