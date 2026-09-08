import log

from slack_sdk.errors import SlackApiError

from utility import select_quals,slack_user_id_from_context,command_words_from_context,authorized
from utility import slack_ts_from_context,user_from_slack_id,qual_strength,normalize_slack_id
from utility import song_qual_counts_by_part,seconds_to_colon_separated,colon_separated_to_seconds
import utility

import functools
import json
import datetime

from data_models import users,parts,songparts,songs,tags,quals,gigs
from data_models import songs_by_id, parts_by_id, users_by_id, slack_name_from_db_name
from data_models import slack_id_from_slack_name, user_id_from_slack_id,slack_id_from_user_id
from data_models import create_qual, delete_qual, create_user, reorder_tags, delete_tag, tag_from_id
from data_models import create_song, create_gig
from data_models import QualExpirationData


#TODO maybe use decorator to automatically register Commands and SubCommands?

# use _commands dict to track what commands have what "path"s

class Command():
    '''
    Command is a bot command.  Commands can have SubCommands, and each command word may have one or more synonyms
    We'll us the tree of commands/sub-commands to parse each command message.  If mapping from message to command
    is certain, we'll just execute it.  If not, user will be prompted for additional clarity.
    '''

    def __init__(self,func,path,auths):
        self.func = func
        self.path = path
        self.auths = auths
        self.help_line = func.__doc__.split("\n")[0]
        self.full_help = func.__doc__
        self.subcommands = {}

    def add_subcommand(self,cmd):
        # basic for now.  will want to figure out synonyms
        self.subcommands[cmd.path] = cmd

    @log.logger.catch
    def run(self,arguments,context,client,**kwargs):
        # probably need args
        if not self.func:
            log.slack.error(f"command object {self.path} called with no function")
            return None
        user = slack_user_id_from_context(context)
        if not self.auths or any([authorized(user,x) for x in self.auths]):
            return self.func(arguments,context,client,**kwargs)
        user_name = users_by_id[user_id_from_slack_id[user]].name
        log.slack.warning(f"command {self.path} called by {user_name} without auth.  (Needs one of {self.auths})")
        return None

    def __eq__(self,other):
        return self.func == other.func


_root_command = {}
all_auths = set()

# decorator for command functions
def command(path,auths):
    #@functools.wraps(f)
    for auth in auths:
        all_auths.add(auth)

    def inner_decorator(f):
        path_list = path.split()
        root = _root_command
        while len(path_list) > 1:
            path_level = path_list.pop(0)
            root = root[path_level].subcommands
        cmd = Command(f,path,auths)
        root[path_list[0]] = cmd
        return f
    return inner_decorator


def iter_commands(user=None,command_words=[],root=_root_command,path=[]):
    '''
        iterate through commands/subcommands that a user has authorization to execute
        if user is given, limit commands to those authorized for the user
        if command_words is given, limit commands that fit the command words
    '''
    for key in sorted(root.keys()):
        cmd = root[key]
        yield from iter_commands(user=user,command_words=command_words,root=cmd.subcommands,path=path)
        yield cmd


@log.logger.catch
def handle_submit(context,client,say=None,respond=None):
    if context['type'] != 'view_submission':
        log.slack.error(f"handle_submit called for non-form-submission.\n{context}")
        return False
    callback_id = context['view']['callback_id']
    submit_func_name = f"{callback_id}_submit"
    submit_func = globals().get(submit_func_name,None)
    if submit_func is None:
        log.error(f"handle_submit: Submit Function '{submit_func_name}' not found.  Punt")
        return False

    # do common things here for all submits
    parent_view_id = context['view']['external_id']
    private_metadata = context['view']['private_metadata']
    if private_metadata:
        metadata = json.loads(context['view']["private_metadata"])
        channel_say = functools.partial(say,channel=metadata['channel_id'],thread_ts=metadata['thread_ts'])
    else:
        log.debug(f"handle_submit: private_metadata not specified.")
        channel_say = say

    results = submit_func(context,client,say=channel_say,respond=respond)
    if type(results) == dict:
        redraw_func = results.get('redraw',None)
        errors = results.get('errors',{})
        kwargs = results.get('kwargs',{})
        if errors:
            return errors
        # This may be required for others, but it's breaking gig_manage.
        # should now pass on via kwargs are in result
        if parent_view_id:
            log.debug(f"parent_view_id is set, so inject: {parent_view_id}")
            kwargs['view_id'] = parent_view_id
        if redraw_func:
            redraw_func(context,client,**kwargs)
    else:
        if results:
            log.debug(f"handle_submit: results of submit func: {results}")

@log.logger.catch
def dispatch(context,client,say=None,respond=None):
    '''
    look through commands from _root_command and execute if sure, or ask for clarification
    '''
    command_words = command_words_from_context(context)
    user = slack_user_id_from_context(context)
    log.debug(f"dispatch_command: user: {user} Command_words{command_words}")
    if command_words:
        root = _root_command
        cmd = None
        while len(command_words) > 0:
            token = command_words.pop(0)
            token_lower = token.lower()
            if token_lower in root:
                cmd = root[token_lower]
                root = cmd.subcommands
            else:
                # didn't match the next word, so pass that on as args
                command_words.insert(0,token)
                break
        if cmd:
            return cmd.run(command_words,context,client,say=say,respond=respond)
        preamble = "Command not recognized.  Here are the commands you can run:"
    else:
        preamble = "Here are the commands you can run:"

    # if haven't dispatched to a command, print help info
    return _root_command['help'].run(command_words,context,client,say=say,respond=respond,help_preamble=preamble)

def output_list_buttons(say, options, preamble=None, context=None):
    '''
    options is a list, each item a tuple of expanatory text, a button label, and a button value
        button label and value are optional.  (If button not specified, there is no button.  If value
        is not specified, the value is the same as the label.)
    output a response listing the options, with buttons and an optional preamble explanation
    '''
    if context:
        ts = slack_ts_from_context(context)
    else:
        ts = None
    lines = []
    blocks = []
    if preamble:
        lines.append(preamble)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": preamble},
            })

    for line_parts in options:
        text = line_parts.pop(0)
        if text == '':
            text = ' '
        if line_parts:
            button_label = line_parts.pop(0)
        else:
            button_label = None
        if line_parts:
            button_value = line_parts.pop(0)
        else:
            button_value = None
        lines.append(f"{text}   '{button_label}' '{button_value}'")
        block = {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
            }
        if button_label:
            block['accessory'] = {
                "type": "button",
                "text": {"type": "plain_text", "text": button_label},
                "action_id": "output-list-button"
                }
        if button_value:
            block['accessory']["value"] = button_value
        blocks.append(block)
    lines_text = "\n".join(lines)
    try:
        if ts is not None:
            say(text=lines_text,blocks=blocks,thread_ts=ts)
        else:
            say(text=lines_text,blocks=blocks)
    except:
        log.error(f"error saying from output_list_buttons:\nblocks={blocks}\ntext={text}")

def command_error(error_msg,context,client,say=None,respond=None):
    user = slack_user_id_from_context(context)
    log.slack.error(f"User {user} getting error: {error_msg}")
    output_list_buttons(say,[],error_msg,context=context)


@command("help",[])
def bot_help(arguments,context,client,say=None,respond=None,help_preamble=""):
    ''' Print bot help
        extra doc string info here
    '''
    user = slack_user_id_from_context(context)
    lines = []
    for cmd in iter_commands():
        if not cmd.auths or any([authorized(user,x) for x in cmd.auths]):
            if cmd.help_line == '__no_help__':
                continue
            lines.append([f"{cmd.help_line}",cmd.path])
        else:
            pass
    output_list_buttons(say,lines,help_preamble,context=context)

#@command("auth",["auth_fail"])
def auth_test(arguments,context,client,say=None,respond=None):
    ''' Test bot dispatch
        extra doc string info here
    '''
    log.debug(f"auth_test invoked")

@command("singer",['music_team'])
def singer_info(arguments,context,client,say=None,respond=None):
    ''' show PRE info for another user
        extra doc string info here
    '''
    user_id = user_from_slack_id(arguments[0])
    if user_id:
        user = users_by_id[user_id]
        pre_self([],context,client,say=say,respond=respond,singer=user)
    else:
        output_list_buttons(say,[],f"Unable to find singer {arguments[0]}",context=context)

@command("pre",[])
def pre_self(arguments,context,client,say=None,respond=None,singer=None):
    ''' show your own PRE info
        extra doc string info here
    '''
    '''
    Here is code for coloring elements (to use for expiration)

    {
      "blocks": [
        {
          "type": "rich_text",
          "elements": [
            {
              "type": "rich_text_section",
              "elements": [
                {
                  "type": "text",
                  "text": "This is normal text, but the next part will be "
                },
                {
                  "type": "color",
                  "value": "#F405B3",
                  "style": {
                    "bold": True
                  },
                  "elements": [
                    {
                      "type": "text",
                      "text": "custom colored text!"
                    }
                  ]
                }
              ]
            }
          ]
        }
      ]
    }

    '''
    slack_id = slack_user_id_from_context(context)
    user_id = user_from_slack_id(slack_id)
    user = users_by_id[user_id]
    if singer is not None:
        user = singer
        slack_id = slack_id_from_user_id(user.id)
    lines = []
    qual_songs = {}
    for qual in select_quals(quals,user_ids=[user.id],only_most_recent=True):
        if qual.song_id in qual_songs:
            qual_song = qual_songs[qual.song_id]
        else:
            qual_song = {}
            qual_songs[qual.song_id] = qual_song
        part = parts_by_id[qual.part_id]
        qual_song[part.name] = QualExpirationData(qual)
    for song_id in qual_songs:
        part_data = " ".join([f"{exp.emoji}{part}" for part,exp in qual_songs[song_id].items()])
        details = f"pre details {slack_id} {song_id}"
        lines.append([f"{songs_by_id[song_id].name}: {part_data}","details",details])
    output_list_buttons(say,lines,f"The following songs are qualified for {user.name}",context=context)

@command("pre delete",['music_team'])
def pre_delete(arguments,context,client,say=None,respond=None,singer=None):
    '''__no_help__
       delete PRE qual record for a specific qual_id
    '''
    user_slack_id = slack_user_id_from_context(context)
    slack_id,escaped_slack_id = normalize_slack_id(user_slack_id)
    if not authorized(user_slack_id,'music_team'):
        msg = f"'pre delete' operation requires music_team authorization."
        return command_error(msg,context,client,say,respond)
    log.debug(f"pre delete called with {arguments}, singer={singer}") 
    if len(arguments) != 1:
        msg = f"'pre delete' operation requires one arg: <qual_id>.  got {arguments}"
        return command_error(msg,context,client,say,respond)

    # gather info for logging delete request
    qual_id = int(arguments[0])
    qual = [q for q in quals if q.id==qual_id][0]
    song = songs_by_id[qual.song_id]
    user = users_by_id[qual.user_id]
    part = parts_by_id[qual.part_id]
    log.music_team.info(f"User {escaped_slack_id} deleting qual record {qual.id} ({user.name} {part.name} {song.name} {qual.date_time.strftime('%m/%d/%y')})")
    delete_qual(qual)

@command("pre details",[])
def pre_details(arguments,context,client,say=None,respond=None,singer=None):
    ''' show PRE info for a specific user and song
        extra doc string info here
    '''
    log.debug(f"pre details called with {arguments}, singer={singer}") 
    if len(arguments) != 2:
        return command_error(f"'pre details' requires two args, <slack_user> and <song_id>",context,client,say,respond)
    singer_slack_id,song_id = arguments
    song_id = int(song_id)
    user_slack_id = slack_user_id_from_context(context)
    if singer_slack_id != user_slack_id:
        # getting someone else's info, so need 'music_team' auth
        if not authorized(user_slack_id,'music_team'):
            return command_error(f"'pre details' for another user requires music_team authorization.",context,client,say,respond)
    singer_id = user_from_slack_id(singer_slack_id)
    if singer is None:
        singer = users_by_id[singer_id]
    else:
        singer_slack_id = slack_id_from_user_id(singer.id)
    lines = []
    qual_list = []
    exp_list = []
    for qual in select_quals(quals,user_ids=[singer_id],song_id=song_id,only_most_recent=False):
        exp_list.append(QualExpirationData(qual))
    for exp in sorted(exp_list,reverse=True):
        delete = f"pre delete {exp.qual.id}"
        lines.append([f"{exp.qual.date_time.strftime('%m/%d/%y')} {parts_by_id[exp.qual.part_id].name} {exp.emoji} {exp.status}","delete",delete])
    output_list_buttons(say,lines,f"Quals for {singer.name} for {songs_by_id[song_id].name}",context=context)

def gig_manage_blocks(list_all=False):
    blocks = []
    if list_all:
        display_gigs = sorted(gigs)
    else:
        display_gigs = [x for x in sorted(gigs) if x.active]
    for gig in display_gigs:
        blocks.extend(manage_blocks([(f"{gig.description} {gig.date_time.strftime('%m/%d/%y')}",
            [('manage',f'manage gig {gig.id}')])]))
    return blocks

@command("manage",[])
def manage_command(arguments,context,client,say=None,respond=None):
    '''__no_help__
       placeholder for manage subcommands, use for subcommand typo resolution
    '''
    log.debug(f"manage called with {arguments}") 
    if len(arguments):
        arg_string = " ".join(arguments)
        return command_error(f"'manage {arg_string}' is not a valid command.  'help' for legal commands",context,client,say,respond)
    return command_error(f"'manage' requires a sub-command.  'help' for legal commands",context,client,say,respond)

@command("manage gigs",[])
def gigs_manage(arguments,context,client,say=None,respond=None):
    ''' Manage Gigs
    '''
    if "trigger_id" not in context:
        # output button so we have a trigger, which is required by slack to do modals
        output_list_buttons(say,
                [[f"Slack requires that you press this button to manage gigs","manage gigs"]],
                None,
                context=context)
        return
    private_metadata = {}
    private_metadata['channel_id'] = context['channel']['id']
    private_metadata['thread_ts'] = context['container']['thread_ts']

    blocks = gig_manage_blocks()

    client.views_open(
        # Pass a valid trigger_id within 3 seconds of receiving it
        trigger_id=context["trigger_id"],
        # View payload
        view={
            "type": "modal",
            # View identifier
            "callback_id": "manage gigs",
            "title": {"type": "plain_text", "text": "Manage Gigs"},
            #"submit": {"type": "plain_text", "text": "Submit"},
            "private_metadata": json.dumps(private_metadata),
            "blocks": blocks
        }
    )

def part_assignment_select(song_id,part,assignments):
    block = {
			"type": "input",
            "block_id": f"part_assignment_select_{song_id}_{part.id}",
			"element": {
				"type": "static_select",
				"placeholder": {
					"type": "plain_text",
					"text": f"Select a {part.name}",
					"emoji": True
				},
				"options": [],
				"action_id": "song_part_select-action"
			},
			"label": {
				"type": "plain_text",
				"text": f"{part.name}",
				"emoji": True
			},
			"optional": False
		}
    selected = None
    for assignment in assignments:
        singer_name = users_by_id[assignment.user_id].name
        menu_text = f"{singer_name} {assignment.emoji()}"
        menu_value = f"{assignment.id}"
        #if selected is None and assignment.sort_order < 2:
        if selected is None:
            selected = (menu_text,menu_value)
        block["element"]["options"].append(
                {
                    "text": {
                        "type": "plain_text",
                        "text": menu_text,
                        "emoji": True
                    },
                    "value": menu_value
                }
            )
    if selected is not None:
        (menu_text,menu_value) = selected
        block["element"]["initial_option"] = {
                "text": {
                    "type": "plain_text",
                    "text": menu_text,
                    "emoji": True
                },
                "value": menu_value
            }
    if len(assignments) == 0:
        block["element"]["options"].append(
                {
                    "text": {
                        "type": "plain_text",
                        "text": "No qualified singers",
                        "emoji": True
                    },
                    "value": "0"
                }
            )
    return block

def setlist_item_blocks(gig,item):
    blocks = []
    if item.song_id is not None:
        # print song with control buttons, and lists for each part.
        song = songs_by_id[item.song_id]
        menu_list = []
        accessory_list = []
        # TODO dynamically assign options, because of limit of 5 from slack
        for label,action in [
                ("edit","edit"),
                ("move up","sort_up"),
                ("move down","sort_down"),
                ("add to setlist","perform"),
                ("remove from setlist","unperform"),
                ("delete from this gig","delete"),
                ]:
            # TODO: add logic here to only have add/delete if in opposite state
            if item.in_setlist and action == "perform":
                continue
            if not item.in_setlist and action == "unperform":
                continue
            accessory_list.append((f"{label}",f"manage setlist_item {action} {item.id}"))
        # now add text for part assignments
        part_text_list = []
        for song_part in sorted(song.parts):
            part = song_part.part
            part_assignments = sorted([a for a in gig.assignments if a.part_id == part.id and a.song_id == item.song_id])
            #blocks.append(part_assignment_select(item.song_id,part,part_assignments))
            if len(part_assignments):
                #part_text_list.append(f"{part.name}: " + "/".join([f"{users_by_id[a.user_id].name}{a.emoji()}({a.sort_order})" for a in part_assignments]))
                assigned = part_assignments[0]
                part_text_list.append(f"{users_by_id[assigned.user_id].name.split()[0]}{assigned.emoji()}")
            else:
                part_text_list.append(" ")
            #print(f"{song.name}  {part}  {assignments}")
        part_text = "/".join(part_text_list)
        comment_text = f"\n{item.comments}" if item.comments else ""
        menu_list.append((f"*{song.name}* {item.song_key} {seconds_to_colon_separated(item.duration)}{comment_text}\n{part_text}",accessory_list))
        blocks.extend( manage_blocks(menu_list) )
    else:
        if item.comments == "-- END SETLIST --":
            blocks.extend( manage_blocks([(f"*{item.comments}* duration: {seconds_to_colon_separated(gig.setlist_duration())} More setlist commands ---->",
                [("add song",f"manage gig add_song {gig.id}")])]))
        else:
            #print comments along with reordering commands
            blocks.extend( manage_blocks([(f"*{item.comments}* duration:{item.duration}",
                [("delete",f"manage setlist_item delete {item.id}")])]))

    return blocks


def gig_blocks(gig):
    blocks = [  
            {
                "type": "input",
                "block_id": "gig_description",
                "element": {
                    "type": "plain_text_input",
                    "initial_value": f"{gig.description}",
                    "action_id": "edit_gig_description-action"
                },
                "label": {
                    "type": "plain_text",
                    "text": "Description",
                    "emoji": True
                },
                "optional": False
            },
            {
                "type": "input",
                "block_id": "gig_location",
                "element": {
                    "type": "plain_text_input",
                    "initial_value": f"{gig.location}",
                    "action_id": "edit_gig_location-action"
                },
                "label": {
                    "type": "plain_text",
                    "text": "Location",
                    "emoji": True
                },
                "optional": False
            },
            {
                "type": "input",
                "block_id": "gig_duration",
                "element": {
                    "type": "plain_text_input",
                    "initial_value": f"{gig.duration}",
                    "action_id": "edit_gig_duration-action"
                },
                "label": {
                    "type": "plain_text",
                    "text": "Desired Set Length (minutes)",
                    "emoji": True
                },
                "optional": False
            },
            {
                "type": "input",
                "block_id": "gig_comments",
                "element": {
                    "type": "plain_text_input",
                    "initial_value": f"{gig.comments}",
                    "action_id": "edit_gig_comments-action"
                },
                "label": {
                    "type": "plain_text",
                    "text": "Comments",
                    "emoji": True
                },
                "optional": False
            },
            {
                "type": "input",
                "block_id": "gig_datetimepicker",
                "element": {
                    "type": "datetimepicker",
                    "initial_date_time": int(gig.date_time.timestamp()),
                    "action_id": "datetimepicker-action"
                },
                "label": {
                    "type": "plain_text",
                    "text": "Time of Gig",
                    "emoji": True
                },
                "optional": True
            }
        ]
    blocks.append(multi_user_select_block(selected=gig.singers,label="Singers"))
    for setlist_item in sorted(gig.setlist):
        blocks.extend(setlist_item_blocks(gig,setlist_item))
    return blocks

def update_manage_gig(context,client,view_id=None,gig=None):
    if gig is None:
        log.warning(f"update_manage_gig called without kwargs that include 'gig' param.")
        submit_title = context['view']['title']['text']
        submit_title_id = int(submit_title.split()[-1])
        # do something with this later if we have to.  For now, punt.
        return
    #log.debug(f"update_manage_gig: context:{context}")
    blocks = gig_blocks(gig)
    if view_id is None:
        log.warning(f"update_manage_gig: view_id is none, so using current")
        view_id = context['view']['id']
    log.debug(f"update_manage_gig: view_id={view_id}")
    client.views_update(
        #trigger_id=context["trigger_id"],
        view_id=view_id,
        view={
            "type": "modal",
            # View identifier
            "callback_id": "manage_gig",
            "title": {"type": "plain_text", "text": f"Manage Gig id {gig.id}"},
            "submit": {"type": "plain_text", "text": "Submit"},
            #"private_metadata": json.dumps(private_metadata),
            "blocks": blocks
        }
    )

@command("manage gig",[])
def manage_gig(arguments,context,client,say=None,respond=None):
    '''__no_help__
       Manage gig
    '''
    log.debug(f"manage_gig called with {arguments}") 
    log.debug(f"manage_gig context {context}") 
    log.debug(f"manage_gig trigger_id {context['trigger_id']}") 
    if "trigger_id" not in context:
        # output button so we have a trigger, which is required by slack to do modals
        output_list_buttons(say,
                [[f"Slack requires that you press this button to manage the gig",f"manage gig {' '.join(arguments)}"]],
                None,
                context=context)
        return
    log.trace(f"gigs = {gigs}") 
    gig_id = int(arguments[0])
    gig_list = [x for x in gigs if x.id == gig_id]
    if len(gig_list) != 1:
        log.error(f"Problem finding gig from id {gig_id}.  Found {gig_list}")
    gig = gig_list[0]
    blocks = gig_blocks(gig)
    #blocks.append(difficulty_select(selected=song.difficulty))
    #blocks.append(tag_select_block(song.tags))
    #log.debug(f"manage_gig: blocks={blocks}")
    view={
        "type": "modal",
        "notify_on_close": True,
        "callback_id": "manage_gig",
        "title": {"type": "plain_text", "text": f"Manage Gig id {gig.id}"},
        "submit": {"type": "plain_text", "text": "Submit"},
        #"private_metadata": json.dumps(private_metadata),
        "blocks": blocks
    }
    if 'view' in context:
        # came from a parent window
        view['external_id'] = context['view']['id']
        modal_open_func = client.views_push
    else:
        # direct from button
        modal_open_func = client.views_open
    modal_open_func(
        trigger_id=context["trigger_id"],
        view_id="manage_gig",
        view=view
    )

def manage_gig_submit(context,client,say=None,respond=None):
    #log.debug(f"context: {context}")
    values = context['view']['state']['values']
    log.debug(f"values: {values}")
    gig_id = int(context['view']['title']['text'].split()[-1])
    gig = [g for g in gigs if g.id == gig_id][0]

    description = values['gig_description']['edit_gig_description-action']['value']
    location = values['gig_location']['edit_gig_location-action']['value']
    duration = int(values['gig_duration']['edit_gig_duration-action']['value'])
    comments = values['gig_comments']['edit_gig_comments-action']['value']
    date_time_timestamp = values['gig_datetimepicker']['datetimepicker-action']['selected_date_time']
    date_time = datetime.datetime.fromtimestamp(date_time_timestamp)
    selected_user_slack_ids = values['multi_users_select']['multi_users_select-action']['selected_users']
    singer_ids = [user_id_from_slack_id[x] for x in selected_user_slack_ids]
    singers = [users_by_id[x] for x in singer_ids]
    gig.update(description=description,location=location,duration=duration,
            comments=comments,date_time=date_time,singers=singers)

    return {'redraw': update_manage_gig, 'kwargs': {'gig': gig}}


    selected = dict()
    for key,value in values.items():
        if key.startswith("part_assignment_select"):
            key_parts = key.split("_")
            song_id = int(key_parts[-2])
            part_id = int(key_parts[-1])
            selected_id = int(values[key]['song_part_select-action']['selected_option']['value'])
            selected[part_id] = selected_id
        else:
            log.debug(f"unhandled value from part_select_submit {key}")
    # error checking
    selected_user_ids = set()
    errors = {}
    for part_id,selected_id in selected.items():
        assignment = [a for a in gig.assignments if a.id == selected_id][0]
        if assignment.user_id in selected_user_ids:
            block_id = f"part_assignment_select_{song_id}_{part_id}"
            errors[block_id] = "Can't assign same person to multiple parts."
        else:
            selected_user_ids.add(assignment.user_id)
    if errors:
        return {'errors':errors}

    song = songs_by_id[song_id]
    for part in sorted(song.parts):
        selected_id = selected[part.part.id]
        part_assignments = sorted([a for a in gig.assignments if a.part_id == part.part.id and a.song_id == song.id])
        if part_assignments[0].id != selected_id:
            # selection changed, so reorder
            for index,assignment in enumerate(part_assignments):
                if assignment.id != selected_id:
                    if assignment.sort_order < index+1:
                        assignment.update(sort_order=index+1)
                else:
                    assignment.update(sort_order=0)

    return {'redraw': update_manage_gig, 'kwargs': {'gig': gig}}

def gig_setlist_item_edit_dialog(gig,item,context,client):
    blocks = [
            {
                "type": "section",
                "text": {
                    "type": "plain_text",
                    "text": f"Edit setlist item",
                    "emoji": True
                }
            },
            {
                "type": "input",
                "block_id": "comments",
                "element": {
                    "type": "plain_text_input",
                    "initial_value": f"{item.comments}",
                    "action_id": "setlist_item_edit-action"
                },
                "label": {
                    "type": "plain_text",
                    "text": "Comments",
                    "emoji": True
                },
                "optional": False
            },
            {
                "type": "input",
                "block_id": "duration",
                "element": {
                    "type": "plain_text_input",
                    "initial_value": f"{seconds_to_colon_separated(item.duration)}",
                    "action_id": "setlist_item_edit-action"
                },
                "label": {
                    "type": "plain_text",
                    "text": "Comments",
                    "emoji": True
                },
                "optional": False
            },
        ]
    if item.song_id:
        song = songs_by_id[item.song_id]
        blocks[0]["text"]["text"] = f"Edit setlist entry for {song.name}"
        # don't normally need comments for song
        blocks[1]["optional"] = True
        blocks.append(
                {
                    "type": "input",
                    "block_id": "song_key",
                    "element": {
                        "type": "plain_text_input",
                        "initial_value": f"{item.song_key}",
                        "action_id": "setlist_item_edit-action"
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "Key",
                        "emoji": True
                    },
                    "optional": False
                },
            )
        for part in sorted(song.parts):
            part_assignments = sorted([a for a in gig.assignments if a.part_id == part.part.id and a.song_id == item.song_id])
            blocks.append(part_assignment_select(item.song_id,part.part,part_assignments))
    log.debug(f"gig_song_part_assign: blocks={blocks}")
    client.views_push(
        trigger_id=context["trigger_id"],
        view_id="manage_setlist_item_edit",
        # View payload
        view={
            "type": "modal",
            "external_id": context['view']['id'],
            "notify_on_close": True,
            "callback_id": "setlist_item_edit",
            "title": {"type": "plain_text", "text": f"Edit Setlist Item {item.id}"},
            "submit": {"type": "plain_text", "text": "Submit"},
            #"private_metadata": json.dumps(private_metadata),
            "blocks": blocks
        }
    )

def setlist_item_edit_submit(context,client,say=None,respond=None):
    #log.debug(f"context: {context}")
    values = context['view']['state']['values']
    log.debug(f"values: {values}")
    item_id = int(context['view']['title']['text'].split()[-1])
    gig = [g for g in gigs if item_id in [si.id for si in g.setlist]][0]
    item = [i for i in gig.setlist if i.id == item_id][0]
    comments = values['comments']['setlist_item_edit-action']['value']
    duration = colon_separated_to_seconds(values['duration']['setlist_item_edit-action']['value'])
    if item.song_id:
        song_key = values['song_key']['setlist_item_edit-action']['value']
    item.update(comments=comments,song_key=song_key,duration=duration)
    if item.song_id:
        selected = dict()
        for key,value in values.items():
            if key.startswith("part_assignment_select"):
                key_parts = key.split("_")
                song_id = int(key_parts[-2])
                part_id = int(key_parts[-1])
                selected_id = int(values[key]['song_part_select-action']['selected_option']['value'])
                selected[part_id] = selected_id
            else:
                log.debug(f"unhandled value from part_select_submit {key}")
        # error checking
        selected_user_ids = set()
        errors = {}
        for part_id,selected_id in selected.items():
            assignment = [a for a in gig.assignments if a.id == selected_id][0]
            if assignment.user_id in selected_user_ids:
                block_id = f"part_assignment_select_{song_id}_{part_id}"
                errors[block_id] = "Can't assign same person to multiple parts."
            else:
                selected_user_ids.add(assignment.user_id)
        if errors:
            return {'errors':errors}

        song = songs_by_id[song_id]
        for part in sorted(song.parts):
            selected_id = selected[part.part.id]
            part_assignments = sorted([a for a in gig.assignments if a.part_id == part.part.id and a.song_id == song.id])
            if part_assignments[0].id != selected_id:
                # selection changed, so reorder
                for index,assignment in enumerate(part_assignments):
                    if assignment.id != selected_id:
                        if assignment.sort_order < index+1:
                            assignment.update(sort_order=index+1)
                    else:
                        assignment.update(sort_order=0)

    # TODO - zzz  make sure gig view updates properly
    return {'redraw': update_manage_gig, 'kwargs': {'gig': gig}}

@command("manage gig add_song",[])
def manage_gig_add_song(arguments,context,client,say=None,respond=None):
    '''__no_help__
       Add song to setlist of gig
    '''
    log.debug(f"manage_gig_add_song called with {arguments}") 
    #log.trace(f"gigs = {gigs}") 
    gig_id = int(arguments[0])
    gig_list = [x for x in gigs if x.id == gig_id]
    if len(gig_list) != 1:
        log.error(f"Problem finding gig from id {gig_id}.  Found {gig_list}")
    gig = gig_list[0]
    blocks = [ song_select_menu([]) ]

    #log.debug(f"blocks={blocks}")
    client.views_push(
        trigger_id=context["trigger_id"],
        view_id="manage_gig_add_song",
        # View payload
        view={
            "type": "modal",
            "external_id": context['view']['id'],
            "notify_on_close": True,
            "callback_id": "manage_gig_add_song",
            "title": {"type": "plain_text", "text": f"Add Song to Gig {gig.id}"},
            "submit": {"type": "plain_text", "text": "Submit"},
            #"private_metadata": json.dumps(private_metadata),
            "blocks": blocks
        }
    )

def manage_gig_add_song_submit(context,client,say=None,respond=None):
    #log.debug(f"context: {context}")
    values = context['view']['state']['values']
    log.debug(f"values: {values}")
    gig_id = int(context['view']['title']['text'].split()[-1])
    gig = [g for g in gigs if g.id == gig_id][0]
    song_id = int(values['songs_select']['song_selected']['selected_option']['value'])
    gig.add_setlist_item(song_id=song_id)
    return {'redraw': update_manage_gig, 'kwargs': {'gig': gig}}

@command("manage setlist_item",[])
def manage_setlist_item(arguments,context,client,say=None,respond=None):
    '''__no_help__
       Manage Setlist Item
    '''
    log.debug(f"manage_setlist_item called with {arguments}") 
    item_id = int(arguments[1])
    gig = [g for g in gigs if item_id in [si.id for si in g.setlist]][0]
    item = [i for i in gig.setlist if i.id == item_id][0]
    end_setlist_item = [i for i in gig.setlist if i.comments == '-- END SETLIST --'][0]
    setlist = sorted(gig.setlist)

    if arguments[0] == "edit":
        gig_setlist_item_edit_dialog(gig,item,context,client)
        return
    elif arguments[0] == "delete":
        gig.delete_setlist_item(item)
        update_manage_gig(context,client,gig=gig)
        return
    elif arguments[0] in ['sort_up','sort_down']:
        insert_point = setlist.index(item)
        offset = 1 if arguments[0] == 'sort_down' else -1
    elif arguments[0] in ['perform','unperform']:
        insert_point = setlist.index(end_setlist_item)
        offset = 0 if arguments[0] == 'unperform' else 0
        in_setlist = False if arguments[0] == 'unperform' else True
        item.update(in_setlist=in_setlist)
    else:
        log.error(f"manage_setlist_item unexpected subcommand: {arguments}")
        return

    # continue logic for sort and perform ops
    log.debug(f"setlist re-sort.  insert_point={insert_point}, offset={offset}  setlist={setlist}")
    setlist.remove(item)
    setlist.insert(insert_point+offset,item)
    log.debug(f"setlist after re-sort.  setlist={setlist}")
    #gig.setlist=setlist
    for index,item in enumerate(setlist):
        if item.sort_order != index:
            item.update(sort_order=index)
    update_manage_gig(context,client,gig=gig)
    #zzz
    return

    gig_id = int(arguments[0])
    gig_list = [x for x in gigs if x.id == gig_id]
    if len(gig_list) != 1:
        log.error(f"Problem finding gig from id {gig_id}.  Found {gig_list}")
    gig = gig_list[0]
    blocks = gig_blocks(gig)
    #blocks.append(difficulty_select(selected=song.difficulty))
    #blocks.append(tag_select_block(song.tags))
    #log.debug(f"manage_gig: blocks={blocks}")
    client.views_push(
        trigger_id=context["trigger_id"],
        view_id="manage_gig",
        # View payload
        view={
            "type": "modal",
            "external_id": context['view']['id'],
            "notify_on_close": True,
            "callback_id": "manage_gig",
            "title": {"type": "plain_text", "text": f"Manage Gig id {gig.id}"},
            "submit": {"type": "plain_text", "text": "Submit"},
            #"private_metadata": json.dumps(private_metadata),
            "blocks": blocks
        }
    )


@command("gig",[])
def gig_songs(arguments,context,client,say=None,respond=None):
    ''' "gig @singer @singer ..."  show which songs can be performed by the performers
        You can specify more than four singers.
    '''
    lines = []
    acceptable_song_strength = -1
    verbose = False
    ids = []
    for arg in arguments:
        if arg.lower() == 'verbose':
            verbose = True
            acceptable_song_strength = -2
        else:
            id = user_from_slack_id(arg)
            if id is not None:
                ids.append(id)

    #ids = [user_from_slack_id(x) for x in arguments]
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
            if qual_strength < acceptable_song_strength:
                break
            qual_strs = []
            if verbose:
                song_quals = select_quals(id_quals,song_id=song.id,only_most_recent=True)
                for part_name in parts:
                    part = [p for p in song.parts if p.part.name == part_name][0]
                    part_quals = select_quals(song_quals, part_id=part.part_id)
                    if len(ids) > 0:
                        names = set([users_by_id[q.user_id].name for q in part_quals])
                        if len(names) >0:
                            names_str = '/'.join(names)
                        else:
                            names_str = 'None'
                        qual_strs.append(f"{part.part.name}: {names_str}")
                    else:
                        qual_strs.append(f"{part.part.name}: {len(part_quals)}")
            if not performable:
                qual_strs.append(f"{missing_parts}")
            qual_str = " / ".join(qual_strs)
            lines.append(f"{song.name}  {qual_str}")


    output_list_buttons(say,[[x,''] for x in lines],f"gig songs for {arguments}:",context=context)

@command("gig add",[])
def gig_add(arguments,context,client,say=None,respond=None):
    ''' "gigs add @singer @singer ..."  create new gig item for a gig with 
        specific singers.
        You can specify more than four singers.
    '''
    ids = []
    description_words = []
    for arg in arguments:
        id = user_from_slack_id(arg)
        if id is not None:
            ids.append(id)
        else:
            description_words.append(arg)
    if description_words:
        description = " ".join(description_words)
    else:
        description = ""

    #TODO capture thread_ts if message is in the #gigs channel
    gig = create_gig(ids,description=description)

    #TODO go straight from create to manage that gig.

@command("admin",['admin'])
def admin(arguments,context,client,say=None,respond=None):
    '''__no_help__
        misc admin actions.  Not used yet.
    '''
    pass

@command("admin song_auto_part_sort_order",['admin'])
def auto_song_part_sort_order(arguments,context,client,say=None,respond=None):
    ''' impose order on the chaos of default sorting in song parts.  Sort Tenor<Lead<Bari<Bass
    '''
    part_sort_order = { 
            "Tenor" : 1,
            "Lead" : 2,
            "Bari" : 3,
            "Bass" : 4,
            }
    for song in songs:
        for part in song.parts:
            if part.part.name in part_sort_order:
                part.update(sort_order=part_sort_order[part.part.name])
            else:
                log.warning(f"song {song.name} has non-standard part {part.part.namd} skipped in auto_sort_order.")
        if song.duration is None:
            song.duration = 120
        if song.written_key is None:
            song.written_key = "Bb"


@command("admin user",['admin'])
def admin_user(arguments,context,client,say=None,respond=None):
    ''' manage slack and database users
    '''
    log.debug(f"admin user called with {arguments}") 
    if len(arguments) < 1:
        return command_error(f"'admin user' requires one arg: <slack_user>",context,client,say,respond)
    if arguments[0] in ["add","edit"]:
        subcommand = arguments[0]
        if len(arguments) < 2:
            return command_error(f"'admin user (add|edit)' also requires <slack_user>",context,client,say,respond)
        slack_display_id = arguments[1]
    else:
        slack_display_id = arguments[0]
        subcommand = None
    slack_user_id,escaped_slack_id = normalize_slack_id(slack_display_id)
    user_id = user_from_slack_id(slack_user_id)
    if user_id is None:
        msg = f"{slack_display_id} is not in the database.  Click this button to add them, then edit."
        next_subcommand = "add"
    else:
        msg = f"Slack requires you to press this button to access the user edit screen."
        next_subcommand = "edit"
    if subcommand == "add" and user_id is None:
        #need to get name
        try:
            response = client.users_profile_get(user=slack_user_id)
            name = response["profile"]["real_name"]
        except SlackApiError as e:
            log.slack.error(f"Error fetching user profile: {e.response['error']}")
            return command_error(f"Error getting real name of user {slack_display_id}!  Please let an admin know.",
                    context,client,say,respond)
        #do add operation (call datamodels func)
        log.debug(f"response: {response}")
        create_user(name,slack_user_id)
        msg = f"{slack_display_id} added to the database.  Click this button to edit."
        user_id = user_from_slack_id(slack_user_id)
        if user_id is None:
            return command_error(f"Adding user {slack_display_id} failed!  Please let an admin know.",
                    context,client,say,respond)
        # add done, so now we want to edit
        subcommand = "edit"

    if "trigger_id" not in context:
        if subcommand is None:
            subcommand = next_subcommand
        # output button so we have a trigger, which is required by slack to do modals
        output_list_buttons(say,
                [[msg,f"admin user {subcommand} {slack_user_id}"]],
                None,
                context=context)
        return

    user = users_by_id[user_id]

    # now we have everything to open edit screen
    client.views_open(
        # Pass a valid trigger_id within 3 seconds of receiving it
        trigger_id=context["trigger_id"],
        view_id="admin_user_edit",
        # View payload
        view={
            "type": "modal",
            # View identifier
            "callback_id": "admin_user_edit",
            "title": {"type": "plain_text", "text": f"Edit User id {user.id}"},
            "submit": {"type": "plain_text", "text": "Submit"},
            #"private_metadata": json.dumps(private_metadata),
            "blocks": [{
                    "type": "input",
                    "block_id": "user_name",
                    "element": {
                        "type": "plain_text_input",
                        "initial_value": f"{user.name}",
                        "action_id": "rename_user-action"
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "Name",
                        "emoji": True
                    },
                    "optional": False
                },
                auth_checkboxes_block(user.auths.split(',')),
            ]
        }
    )

@command("users",['admin'])
def map_users(arguments,context,client,say=None,respond=None):
    ''' map users from slack (requires rate limited API call)
    '''
    users_to_skip = ['slackbot','caguayo']
    slack_users = client.users_list()['members']
    for user in slack_users:
        if user['deleted'] or user['is_bot'] or user['name'] in users_to_skip:
            continue
        slack_id_from_slack_name[user['profile']['real_name']] = user['id']
    unused_slack_names = [x for x in slack_id_from_slack_name.keys()]
    lines = []
    for user in users:
        if user.name in slack_id_from_slack_name:
            slack_id = slack_id_from_slack_name[user.name]
            user_id_from_slack_id[slack_id] = user.id
            user.set_slack_id(slack_id)
            unused_slack_names.remove(user.name)
        elif ( user.name in slack_name_from_db_name and 
               slack_name_from_db_name[user.name] in slack_id_from_slack_name ):
            user_name = slack_name_from_db_name[user.name]
            slack_id = slack_id_from_slack_name[user_name]
            user_id_from_slack_id[slack_id] = user.id
            user.set_slack_id(slack_id)
            unused_slack_names.remove(user_name)
        else:
            slack_id = None
        lines.append([f"{user.name}  {user.id}  {slack_id}",""])
    lines.append([f"unmapped slack users: {unused_slack_names}",""])
    log.slack.info(lines)

@command("pre pass",['evaluator'])
def pre_pass(arguments,context,client,say=None,respond=None):
    ''' record a PRE pass for a Singer
        expected args: part user song
    '''
    def pre_help():
        output_list_buttons(say,
                [[f"re-enter command or press button to do interactively:","pre interact"]],
                f"Pre Pass command needs arguments <user> <part> <song title words>",
                context=context)
    requester_slack_id = slack_user_id_from_context(context)
    requester_slack_id,escaped_requester_slack_id = normalize_slack_id(requester_slack_id)
    #special precessing for submit from "pre interact"
    args = list(arguments)
    if args and args[0] == '_pre_interact_submit':
        _x,slack_user_id,part,song_id,text_date,channel_id,ts = args
        user = users_by_id[user_from_slack_id(slack_user_id)]
        song = songs_by_id[song_id]
        date = datetime.datetime.strptime(text_date,"%Y-%m-%d")
        qual = create_qual(user,song,part,date)
        channel_say = functools.partial(say,channel=channel_id,thread_ts=ts)
        #TODO: do some notification/congrats of the user
        log.music_team.info(f"PRE qual added for <@{slack_user_id}> {song.name} {part} {qual.date_time.strftime('%m/%d/%y')} by {escaped_requester_slack_id}")
        output_list_buttons(channel_say,[],f"PRE qual added for <@{slack_user_id}> {song.name} {part} {qual.date_time.strftime('%m/%d/%y')}",context=context)
        return
    if len(args) < 3:
        log.debug("need more args")
        pre_help()
        return
    user_id = user_from_slack_id(args.pop(0))
    if user_id is None:
        log.debug("user_id is None")
        return command_error(f"user {arguments[0]} has not been added to the system.  Please ask an admin to do so.",context,client,say,respond)
        #pre_help()
        #return
    user = users_by_id[user_id]
    user_name = user.name
    user_slack_id = arguments[0]
    if user_slack_id.startswith('<@'):
        user_slack_id = user_slack_id[2:-1]

    part = args.pop(0)
    if part not in ['tenor','lead','bari','bass']:
        pre_help()
        return
    if len(args) < 1:
        pre_help()
        return
    lines = []
    relevant_songs = []
    for relevance,song in hint_sorted_songs(args):
        if relevance > 0.99:
            relevant_songs.append(song)
        if relevance > 0.4:
            log.debug([f"{relevance} - {song.name}: {song.id}",""])
            lines.append([f"{song.name}",
                f"pre pass @{user_name} {part}",
                        f"pre pass {user_slack_id} {part} {song.name}" ])
    if len(relevant_songs) == 1:
        song = relevant_songs[0]
        # we have everything we need, and song is obvious, so do it!
        qual = create_qual(user,song,part)
        #TODO: do some notification of the user
        log.music_team.info(f"PRE qual added for <@{slack_user_id}> {song.name} {part} {qual.date_time.strftime('%m/%d/%y')} by {requester_slack_id}")
        output_list_buttons(say,[],f"PRE qual added for <@{user_slack_id}> {song.name} {part} {qual.date_time.strftime('%m/%d/%y')}",context=context)
    else:
        if len(relevant_songs) == 0:
            preamble = f"No songs found matching hints {args}."
        else:
            preamble = f"Matching songs for hints {args}."
        lines.append([f"Didn't get what you wanted?  Press button to do interactively:","pre interact"])
        output_list_buttons(say,lines,preamble,context=context)


@command("pre interact",['evaluator'])
def pre_interact(arguments,context,client,say=None,respond=None):
    ''' record a PRE pass for a Singer Interactively
        expected args: none necessary.  May give hints for user, part, and song.
    '''
    hints = arguments
    songs_select = song_select_menu(hints)
    parts_select = part_select_menu(hints)
    if "trigger_id" not in context:
        # output button so we have a trigger, which is required by slack to do modals
        output_list_buttons(say,
                [[f"Slack requires that you press this button to interact","pre interact "+" ".join(arguments)]],
                None,
                context=context)
        return
    private_metadata = {}
    private_metadata['channel_id'] = context['channel']['id']
    private_metadata['thread_ts'] = context['container']['thread_ts']

    client.views_open(
        # Pass a valid trigger_id within 3 seconds of receiving it
        trigger_id=context["trigger_id"],
        # View payload
        view={
            "type": "modal",
            # View identifier
            "callback_id": "pre interact",
            "title": {"type": "plain_text", "text": "Record PRE Pass"},
            "submit": {"type": "plain_text", "text": "Submit"},
            "private_metadata": json.dumps(private_metadata),
            "blocks": [
                {
                    "type": "input",
                    "block_id": "users_select",
                    "element": {
                        "type": "users_select",
                        "action_id": "user_selected",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Select a Singer",
                        }
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "Singer",
                        "emoji": True
                    },
                },
                songs_select,
                parts_select,
                datepicker_block(),
            ]
        }
    )

def manage_blocks(text_menu_list):
    ''' text_menu_list is a list of 2-tuples: mrkdown_text,[overflow_data]
        overflow_data is also a list of 2-tuples: label,value    value is dispatched as a command
    '''
    blocks = []
    for mrkdown_txt,overflow_list in text_menu_list:
        block = {
			"type": "section",
			"text": {
				"type": "mrkdwn",
				"text": mrkdown_txt
			},
			"accessory": {
				"type": "overflow",
				"action_id": "overflow-action"
                }
            }
        options = []
        for label,value in overflow_list:
            options.append(
					{
						"text": {
							"type": "plain_text",
							"text": label,
							"emoji": True
						},
						"value": value
					}
                )
        block["accessory"]["options"] = options
        blocks.append(block)
    return blocks

def song_by_tag_manage_blocks(alpha_sort=False):
    if alpha_sort:
        sort_key = None
    else:
        sort_key = lambda song: (song.difficulty,song.name)
    blocks = []
    unprinted_songs = sorted(songs)
    for tag in sorted(tags):
        blocks.extend(manage_blocks([(f"*{tag.name}*",[('manage tags','manage tags')])]))
        sort_songs = sorted(tag.songs,key=sort_key)
        for song in sort_songs:
            part_counters = song_qual_counts_by_part(song,quals)
            part_count_txt = "/".join([str(part_counters[name]) for name in part_counters['__part_print_order__']])
            blocks.extend(manage_blocks([(f"\u00A0\u00A0\u00A0\u00A0{song.name} quals: {part_count_txt}  diff:{song.difficulty}",[('edit',f'manage song edit {song.id}')])]))
            if song in unprinted_songs:
                unprinted_songs.remove(song)
    if unprinted_songs:
        blocks.extend(manage_blocks([(f"*<untagged>*",[('manage tags','manage tags')])]))
        for song in unprinted_songs:
            part_counters = song_qual_counts_by_part(song,quals)
            part_count_txt = "/".join([str(part_counters[name]) for name in part_counters['__part_print_order__']])
            blocks.extend(manage_blocks([(f"\u00A0\u00A0\u00A0\u00A0{song.name} quals: {part_count_txt}  diff:{song.difficulty}",[('edit',f'manage song edit {song.id}')])]))
    return blocks

def update_manage_songs(context,client,view_id=None):
    blocks = song_by_tag_manage_blocks()
    if view_id is None:
        log.warning(f"update_manage_songs: view_id is none, so using current")
        view_id = context['view']['id']
    log.debug(f"update_manage_songs: view_id={view_id}")
    client.views_update(
        #trigger_id=context["trigger_id"],
        view_id=view_id,
        view={
            "type": "modal",
            # View identifier
            "callback_id": "manage songs interact",
            "title": {"type": "plain_text", "text": "Manage Song Tags"},
            #"submit": {"type": "plain_text", "text": "Submit"},
            #"private_metadata": json.dumps(private_metadata),
            "blocks": blocks
        }
    )

@command("manage songs",['music_team'])
def manage_songs(arguments,context,client,say=None,respond=None):
    '''Manage songs
       Manage songs to add/delete, or change sort order
    '''
    log.debug(f"manage_songs called with {arguments}") 
    if "trigger_id" not in context:
        # output button so we have a trigger, which is required by slack to do modals
        output_list_buttons(say,
                [[f"Slack requires that you press this button to interact","manage tags"]],
                None,
                context=context)
        return
    private_metadata = {}
    private_metadata['channel_id'] = context['channel']['id']
    private_metadata['thread_ts'] = context['container']['thread_ts']
    blocks = song_by_tag_manage_blocks()
    blocks.extend(manage_blocks([(f"Other Actions",[("Create New Song","manage song create_new")])]))

    client.views_open(
        trigger_id=context["trigger_id"],
        view_id="manage_songs_interact",
        # View payload
        view={
            "type": "modal",
            # View identifier
            "callback_id": "manage songs interact",
            "title": {"type": "plain_text", "text": "Manage Songs"},
            #"submit": {"type": "plain_text", "text": "Submit"},
            "private_metadata": json.dumps(private_metadata),
            "blocks": blocks
        }
    )

def difficulty_select(selected=None):
    block = {
			"type": "input",
            "block_id": "song_difficulty_select",
			"element": {
				"type": "static_select",
				"placeholder": {
					"type": "plain_text",
					"text": "Select an item",
					"emoji": True
				},
				"options": [],
				"action_id": "difficulty_select-action"
			},
			"label": {
				"type": "plain_text",
				"text": "Difficulty",
				"emoji": True
			},
			"optional": False
		}
    if selected is not None:
        block["element"]["initial_option"] = {
                "text": {
                    "type": "plain_text",
                    "text": f"{selected}",
                    "emoji": True
                },
                "value": f"{selected}"
            }
    for diff in range(0,10):
        block["element"]["options"].append(
                {
                    "text": {
                        "type": "plain_text",
                        "text": f"{diff}",
                        "emoji": True
                    },
                    "value": f"{diff}"
                }
            )
    return block

def checkbox_option(text,value=None):
    if value is None:
        value=text
    return {
                "text": {
                    "type": "plain_text",
                    "text": f"{text}",
                    "emoji": True
                },
                #"description": {
                    #"type": "plain_text",
                    #"text": "description",
                    #"emoji": True
                #},
                "value": f"{value}"
            }

def select_option(text,value=None):
    if value is None:
        value=text
    return {
                "text": {
                    "type": "plain_text",
                    "text": f"{text}",
                    "emoji": True
                },
                "value": f"{value}"
            }

def tag_checkbox_option(tag):
    return checkbox_option(tag.name,tag.id)

def tag_select_option(tag):
    return select_option(tag.name,tag.id)

def tag_select_block(selected_tags=[]):
    block = {
			"type": "input",
            "block_id": "song_tag_select",
            "label": {
                "type": "plain_text",
                "text": "Categories",
                "emoji": True,
                },
            "optional": False,
			"element": {
                "type": "multi_static_select",
                "action_id": "tag_select-action"
            }
		}
    if selected_tags:
        block["element"]["initial_options"] = [tag_select_option(t) for t in sorted(selected_tags)]
    block["element"]["options"] = [tag_select_option(t) for t in sorted(tags)][:10]
    return block

def tag_checkboxes_block(selected_tags=[]):
    block = {
			"type": "actions",
            "block_id": "song_tag_checkboxes",
			"elements": [
				{
					"type": "checkboxes",
					"action_id": "tag_checkbox-action"
				}
			]
		}
    if selected_tags:
        block["elements"][0]["initial_options"] = [tag_checkbox_option(t) for t in sorted(selected_tags)]
    block["elements"][0]["options"] = [tag_checkbox_option(t) for t in sorted(tags)][:10]
    return block

def auth_checkboxes_block(selected_auths):
    # get around "" as default in auths
    if "" in selected_auths:
        selected_auths.remove("")
    log.debug(f"selected_auths={selected_auths}")
    block = {
			"type": "actions",
            "block_id": "auth_checkboxes",
			"elements": [
				{
					"type": "checkboxes",
					"action_id": "auth_checkbox-action"
				}
			]
		}
    if selected_auths:
        block["elements"][0]["initial_options"] = [checkbox_option(t) for t in sorted(selected_auths)]
    block["elements"][0]["options"] = [checkbox_option(t) for t in sorted(all_auths)]
    log.debug(f"block={block}")
    return block

def multi_user_select_block(selected=[],label="Users"):
    block = {
			"type": "input",
            "block_id": "multi_users_select",
            "label": {
                "type": "plain_text",
                "text": label,
                "emoji": True,
                },
            "optional": False,
			"element": {
                "type": "multi_users_select",
                "placeholder": {
                    "type": "plain_text",
                    "text": f"select {label}",
                    },
                "action_id": "multi_users_select-action"
            }
		}
    if selected:
        block["element"]["initial_users"] = [f"{x.slack_id}" for x in selected]
    return block

def datepicker_block(date=datetime.date.today()):
    block = {
            "type": "input",
            "block_id": "datepicker",
            "element": {
                "type": "datepicker",
                "initial_date": date.strftime("%Y-%m-%d"),
                "action_id": "datepicker-action"
            },
            "label": {
                "type": "plain_text",
                "text": "Date",
                "emoji": True
            },
            "optional": False
        }
    return block

@command("manage song",['music_team'])
def manage_song(arguments,context,client,say=None,respond=None):
    '''__no_help__
       Manage song to edit
    '''
    log.debug(f"manage_song called with {arguments}") 
    if arguments[0] == 'create_new':
        song = create_song("New Song")
    if arguments[0] in ['edit','create_new']:
        if arguments[0] == 'edit':
            song_id = int(arguments[1])
            song = songs_by_id[song_id]
        #song_lines = '\n'.join(s.name for s in target_tag.songs)
        part_counters = song_qual_counts_by_part(song,quals)
        log.debug(f"part_counters: {part_counters}")
        blocks = [
                    {
                        "type": "input",
                        "block_id": "song_name",
                        "element": {
                            "type": "plain_text_input",
                            "initial_value": f"{song.name}",
                            "action_id": "edit_song-action"
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Name",
                            "emoji": True
                        },
                        "optional": False
                    },
                    {
                        "type": "input",
                        "block_id": "song_written_key",
                        "element": {
                            "type": "plain_text_input",
                            "initial_value": f"{song.written_key}",
                            "action_id": "edit_song-action"
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Notated Key",
                            "emoji": True
                        },
                        "optional": False
                    },
                    {
                        "type": "input",
                        "block_id": "song_performance_key",
                        "element": {
                            "type": "plain_text_input",
                            "initial_value": f"{song.performance_key}",
                            "action_id": "edit_song-action"
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Performance Key",
                            "emoji": True
                        },
                        "optional": True
                    },
                    {
                        "type": "input",
                        "block_id": "song_duration",
                        "element": {
                            "type": "plain_text_input",
                            "initial_value": f"{seconds_to_colon_separated(song.duration)}",
                            "action_id": "edit_song-action"
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Duration",
                            "emoji": True
                        },
                        "optional": False
                    },
                ]
        if song.performance_key is None:
            blocks[2]["element"].pop("initial_value")
        blocks.append(difficulty_select(selected=song.difficulty))
        blocks.append(tag_select_block(song.tags))
        log.debug(f"manage_song: blocks={blocks}")
        log.debug(f"manage_song: setting external_id: {context['view']['id']}")
        client.views_push(
            trigger_id=context["trigger_id"],
            view_id="manage_song_edit",
            # View payload
            view={
                "type": "modal",
                "external_id": context['view']['id'],
                "notify_on_close": True,
                "callback_id": "manage_song_edit",
                "title": {"type": "plain_text", "text": f"Edit Song id {song.id}"},
                "submit": {"type": "plain_text", "text": "Submit"},
                #"private_metadata": json.dumps(private_metadata),
                "blocks": blocks
            }
        )
    else:
        command_error(f"'manage_song' got unsupported args {arguments}",context,client,say,respond)

def manage_song_edit_submit(context,client,say=None,respond=None):
    log.debug(f"values: {context['view']['state']['values']}")
    name = context['view']['state']['values']['song_name']['edit_song-action']['value']
    written_key = context['view']['state']['values']['song_written_key']['edit_song-action']['value']
    performance_key = context['view']['state']['values']['song_performance_key']['edit_song-action']['value']
    if not performance_key or performance_key == "None":
        performance_key = None
    duration = colon_separated_to_seconds(context['view']['state']['values']['song_duration']['edit_song-action']['value'])
    song_id = int(context['view']['title']['text'].split()[-1])
    song = songs_by_id[song_id]
    song_db_tag_ids = [x.id for x in song.tags]
    log.debug(f"name: {name}   id: {song_id} db_tags: {song_db_tag_ids}")
    difficulty = int(context['view']['state']['values']['song_difficulty_select']['difficulty_select-action']['selected_option']['value'])
    log.debug(f"difficulty: {difficulty}")
    selected_categories = context['view']['state']['values']['song_tag_select']['tag_select-action']['selected_options']
    tag_ids = [ int(x['value']) for x in selected_categories ]
    target_tags = [tag_from_id(x) for x in tag_ids]
    log.debug(f"tag_ids: {tag_ids}")
    song.update(name=name,difficulty=difficulty,written_key=written_key,performance_key=performance_key,
            duration=duration,tags=target_tags)
    return {'redraw': update_manage_songs }

def admin_user_edit_submit(context,client,say=None,respond=None):
    values = context['view']['state']['values']
    log.debug(f"values: {values}")
    name = context['view']['state']['values']['user_name']['rename_user-action']['value']
    user_id = int(context['view']['title']['text'].split()[-1])
    user = users_by_id[user_id]
    selected_checkboxes = values['auth_checkboxes']['auth_checkbox-action']['selected_options']
    auths = [ x['value'] for x in selected_checkboxes ]
    log.debug(f"user_id: {user.id}  old_name:{user.name} new_name: {name} auths: {auths}")
    user.update(name=name,auth_list=auths)
    return {} # this says do not refresh parent view


def tag_manage_blocks():
    menu_list = []
    for tag in sorted(tags):
        accessory_list = []
        for label,action in [
                ("move up","sort_up"),
                ("move down","sort_down"),
                ("rename","rename"),
                ("delete","delete"),
                ]:
            accessory_list.append((f"{label}",f"manage tag {action} {tag.id}"))
        menu_list.append((f"*{tag.name}* _({len(tag.songs)} songs)_",accessory_list))
    menu_list.append((f"Other Actions",[("Create New Tag","manage tag create_new")]))
    return manage_blocks(menu_list)

def update_tag_manage_view(context,client,view_id=None):
    blocks = tag_manage_blocks()
    if view_id is None:
        view_id = context['view']['id']
    client.views_update(
        #trigger_id=context["trigger_id"],
        view_id=view_id,
        view={
            "type": "modal",
            # View identifier
            "callback_id": "manage tags interact",
            "title": {"type": "plain_text", "text": "Manage Song Tags"},
            #"submit": {"type": "plain_text", "text": "Submit"},
            #"private_metadata": json.dumps(private_metadata),
            "blocks": blocks
        }
    )

@command("manage tag",['music_team'])
def manage_tag(arguments,context,client,say=None,respond=None):
    '''__no_help__
       Manage song tag to change sort order
    '''
    log.debug(f"manage_tag called with {arguments}") 
    if arguments[0] == 'sort_up' or arguments[0] == 'sort_down':
        if arguments[0] == 'sort_up':
            insert_offset = -1
        else:
            insert_offset = 1
        target_tag_id = int(arguments[1])
        sorted_tag_list = sorted(tags)
        target_tag_index = None
        for index,tag in enumerate(sorted_tag_list):
            if tag.id == target_tag_id:
                target_tag_index = index
                target_tag = tag
                break
        sorted_tag_list.remove(target_tag)
        insertion_index = target_tag_index + insert_offset
        if insertion_index < 0:
            insertion_index = 0
        sorted_tag_list.insert(insertion_index,target_tag)
        reorder_tags(sorted_tag_list)

        # repaint modal with updated sorting
        update_tag_manage_view(context,client)
    elif arguments[0] == 'rename':
        target_tag_id = int(arguments[1])
        for tag in tags:
            if tag.id == target_tag_id:
                target_tag = tag
                break
        song_lines = '\n'.join(s.name for s in target_tag.songs)
        #target_tag.songs
        #client.views_open(
        client.views_push(
            trigger_id=context["trigger_id"],
            view_id="manage_tag_rename",
            # View payload
            view={
                "type": "modal",
                "external_id": context['view']['id'],
                "notify_on_close": True,
                "callback_id": "manage tag rename",
                "title": {"type": "plain_text", "text": f"Rename Tag id {target_tag.id}"},
                "submit": {"type": "plain_text", "text": "Submit"},
                #"private_metadata": json.dumps(private_metadata),
                "blocks": [{
                        "type": "input",
                        "block_id": "tag_name",
                        "element": {
                            "type": "plain_text_input",
                            "initial_value": f"{target_tag.name}",
                            "action_id": "rename_tag-action"
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Name",
                            "emoji": True
                        },
                        "optional": False
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"FYI, this tag is assigned to the following songs.  Either rename appropriately, or fix tag mappings after.\n{song_lines}"
                        },
                    },
                ]
            }
        )
    elif arguments[0] == 'create_new':
        client.views_push(
            trigger_id=context["trigger_id"],
            view_id="manage_tag_create_new",
            # View payload
            view={
                "type": "modal",
                "external_id": context['view']['id'],
                "notify_on_close": True,
                "callback_id": "manage tag create_new",
                "title": {"type": "plain_text", "text": "Create New Song Tag"},
                "submit": {"type": "plain_text", "text": "Submit"},
                #"private_metadata": json.dumps(private_metadata),
                "blocks": [{
                        "type": "input",
                        "block_id": "tag_name",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "create_new_tag-action"
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Name",
                            "emoji": True
                        },
                        "optional": False
                    }
                ]
            }
        )
    elif arguments[0] == 'delete':
        target_tag_id = int(arguments[1])
        for tag in tags:
            if tag.id == target_tag_id:
                target_tag = tag
                break
        if len(target_tag.songs):
            log.slack.error(f"Can't delete song tag that has songs assigned to it.  {target_tag.name}")
        else:
            log.music_team.info(f"Deleting song tag {target_tag.name}.")
            delete_tag(target_tag)
            # repaint modal with updated sorting
            update_tag_manage_view(context,client)
    else:
        command_error(f"'manage_tag' got unsupported args {arguments}",context,client,say,respond)


@command("manage tags",['music_team'])
def manage_tags(arguments,context,client,say=None,respond=None):
    '''Manage tags
       Manage song tags to add/delete, or change sort order
    '''
    log.debug(f"manage_tages called with {arguments}") 
    if "trigger_id" not in context:
        # output button so we have a trigger, which is required by slack to do modals
        output_list_buttons(say,
                [[f"Slack requires that you press this button to interact","manage tags"]],
                None,
                context=context)
        return
    private_metadata = {}
    private_metadata['channel_id'] = context['channel']['id']
    private_metadata['thread_ts'] = context['container']['thread_ts']
    blocks = tag_manage_blocks()

    client.views_open(
        trigger_id=context["trigger_id"],
        view_id="manage_tags_interact",
        # View payload
        view={
            "type": "modal",
            # View identifier
            "callback_id": "manage tags interact",
            "title": {"type": "plain_text", "text": "Manage Song Tags"},
            #"submit": {"type": "plain_text", "text": "Submit"},
            "private_metadata": json.dumps(private_metadata),
            "blocks": blocks
        }
    )


def part_select_menu(hints):
    option_objects = []
    for part_name in ['Tenor','Lead','Bari','Bass']:
        option_objects.append(
            {
                "text": {
                    "type": "plain_text",
                    "text": f"{part_name}"
                    },
                "value": f"{part_name}"
            } 
        )
    return {
        "type": "input",
        "block_id": "parts_select",
        "element": {
            "action_id": "part_selected",
            "type": "static_select",
            "placeholder": {
                "type": "plain_text",
                "text": "Select a part"
            },
            "options": option_objects
        },
        "label": {
            "type": "plain_text",
            "text": "Part",
            "emoji": True
        },

    }

def song_select_menu(hints):
    option_objects = []
    for (relevance,song) in hint_sorted_songs(hints):
        # show all if no hints, otherwise, anything that matches
        if hints and relevance < 0.1:
            break
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
        "type": "input",
        "block_id": "songs_select",
        "element": {
            "action_id": "song_selected",
            "type": "static_select",
            "placeholder": {
                "type": "plain_text",
                "text": "Select a song"
            },
            "options": option_objects
        },
        "label": {
            "type": "plain_text",
            "text": "Song",
            "emoji": True
        },
    }

def hint_sorted_songs(hints):
    '''
        output a list of sorted tuples, each containing (relevance,song)
        relevance is based on the 'hints' list.  Hints can be words in the title,
            song tags, (in the future, other metadata songs may have)
    '''
    def relevance_alpha_sort(x,y):
        if x[0] > y[0]: # relevance
            return 1
        if x[0] < y[0]: # relevance
            return -1
        return 1 if x[1].name > y[1].name else -1 # alpha on song name
        
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
        tuples.append((relevance/hint_count,song))
    tuples.sort(key=functools.cmp_to_key(relevance_alpha_sort))
    return tuples


