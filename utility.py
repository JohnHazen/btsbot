import log

import functools
import itertools
import data_models
import json
import os
import datetime

admin_users = []
DAYS_TO_QUAL_EXPIRATION = 365
WARN_DAYS_PRIOR_TO_QUAL_EXPIRATION = 30
if "SLACK_BTSBOT_USER_ID" in os.environ:
    BTSBOT_USER_ID = os.environ["SLACK_BTSBOT_USER_ID"]
    BTS_BOT_ESCAPED_USER_ID = f'<@{BTSBOT_USER_ID}>'
    log.debug(f"BTSBOT_USER_ID set to {BTSBOT_USER_ID}")
else:
    log.slack.error(f"SLACK_BTSBOT_USER_ID not set in environment.  Invoking commands by @BTS-bot will fail.")
    BTSBOT_USER_ID = "__xxx__"

def normalize_slack_id(slack_id):
    """ take unknown-format slack_id (could be escaped or not), and return tuple of 
            <unescaped>,<escaped>
    """
    if slack_id.startswith('<@'):
        return slack_id[2:-1],slack_id
    return slack_id,f"<@{slack_id}>"

def user_from_slack_id(slack_id):
    slack_id,escaped_slack_id = normalize_slack_id(slack_id)
    if slack_id in data_models.user_id_from_slack_id:
        return data_models.user_id_from_slack_id[slack_id]
    else:
        log.slack.error(f"No (database) user.id for slack user {escaped_slack_id}.  May need to add them to the database.")
        return None

def slack_user_id_from_context(context):
    if "user" in context:
        return context["user"]["id"]
    if "event" in context:
        return context["event"]["user"]
    log.slack.warning(f"Unable to get user id from context: {context}  returning None")
    return None

def slack_ts_from_context(context):
    if "container" in context and 'thread_ts' in context['container']:
        return context["container"]["thread_ts"]
    if "event" in context:
        return context["event"]["ts"]
    #if "actions" in context:
        #return context["actions"][0]['action_ts']
    log.warning(f"Unable to get ts from context: {context}  returning None")
    return None

def command_words_from_context(context):
    if "event" in context:
        command_words = context["event"]["text"].split()
    elif "actions" in context:
        if "selected_option" in context["actions"][0]:
            command_words = context["actions"][0]["selected_option"]["value"].split()
        elif "value" in context["actions"][0]:
            command_words = context["actions"][0]["value"].split()
        else:
            command_words = context["actions"][0]["text"]["text"].split()
    elif context['type'] == 'view_submission':
        slack_user = context['view']['state']['values']['users_select']['user_selected']['selected_user']
        song_id = int(context['view']['state']['values']['songs_select']['song_selected']['selected_option']['value'])
        part = context['view']['state']['values']['parts_select']['part_selected']['selected_option']['value']
        log.debug(context['view']['state']['values'])
        text_date = context['view']['state']['values']['datepicker']['datepicker-action']['selected_date']
        #date = datetime.datetime.strptime(text_date,"%Y-%m-%d").date
        metadata = json.loads(context['view']["private_metadata"])
        command_words = ['pre','pass','_pre_interact_submit',
                        slack_user,part,song_id,text_date,metadata['channel_id'],metadata['thread_ts']]
    else:
        log.warning(f"Unable to get command_words from context: {context}")
        return None
    if command_words[0] == BTS_BOT_ESCAPED_USER_ID:
        bot_id = command_words.pop(0)
    return command_words

def authorized(user,operation):
    # user can be specified as a datatypes.User instance, an int (or int'able string) for user_id
    #  if these fail, use as slack_id
    if operation == "auth_fail":
        return False
    user_obj = None
    if type(user) == data_models.User:
        user_obj = user
    if user_obj is None:
        # try to get object from user_id, if not, it's a slack_id
        try:
            user_id = int(user)
        except:
            user_id = data_models.user_id_from_slack_id[user]
        user_obj = data_models.users_by_id[user_id]
    log.debug(f"authorized: checking user {user_obj.name} for {operation}")
    return user_obj.authorized(operation)
    return False

def setup_expiry():
    global DAYS_TO_QUAL_EXPIRATION, WARN_DAYS_PRIOR_TO_QUAL_EXPIRATION
    try:
        DAYS_TO_QUAL_EXPIRATION = int(os.environ['DAYS_TO_QUAL_EXPIRATION'])
        WARN_DAYS_PRIOR_TO_QUAL_EXPIRATION = int(os.environ['WARN_DAYS_PRIOR_TO_QUAL_EXPIRATION'])
    except:
        log.info(f"using default values:\n  DAYS_TO_QUAL_EXPIRATION = {DAYS_TO_QUAL_EXPIRATION}\n  WARN_DAYS_PRIOR_TO_QUAL_EXPIRATION = {WARN_DAYS_PRIOR_TO_QUAL_EXPIRATION}\nYou can set these in the environment to override.")

def set_admin(slack_id):
    if not admin_users:
        log.warning(f"WARN: Setting admin user to {slack_id}.")
        admin_users.append(slack_id)
        return

def write_round_file(round_qtets,all_qtets,quals,songs):
    """
    hacky write qtet round to a file for printing, since web printing is busted
    """
    round_file = open("current_round.txt","w")
    qtet_number = 0
    print(f"ROUND {len([x for x in all_qtets if x is None])}\n\n",file=round_file)
    for qtet in round_qtets:
        qtet_number += 1
        quals = select_quals(quals,user_ids=[u.id for u in qtet],only_most_recent=True)
        sorted_songs = sorted([(qual_strength(quals,s),s) for s in songs],reverse=True)
        print(f"{qtet_number}: {' / '.join([x.name for x in qtet])}",file=round_file)
        print(f"    songs: {' / '.join([song.name for ((strength,missing),song) in sorted_songs if strength > 0])}\n",file=round_file)
    round_file.close()

def select_quals (
        qual_list,
        user_ids=None,
        exclude_user_id=None,
        song_id=None,
        part_id=None,
        exclude_part_id=None,
        only_most_recent = False):
    reduced_list = [q for q in qual_list]
    if user_ids is not None:
        reduced_list = [q for q in reduced_list if q.user_id in user_ids]
    if exclude_user_id is not None:
        reduced_list = [q for q in reduced_list if q.user_id != exclude_user_id]
    if song_id is not None:
        reduced_list = [q for q in reduced_list if q.song_id == song_id]
    if part_id is not None:
        reduced_list = [q for q in reduced_list if q.part_id == part_id]
    if exclude_part_id is not None:
        reduced_list = [q for q in reduced_list if q.part_id != exclude_part_id]
    if only_most_recent:
        sorted_list = sorted([((q.user_id,q.song_id,q.part_id),q.date_time,q) for q in reduced_list])
        reduced_list = []
        current_ids = current_dt = current_q = None
        for (ids,dt,q) in sorted_list:
            (u_id,s_id,p_id) = ids
            if ids != current_ids:
                log.debug("ids differ, appending")
                #ids index is different, so append last entry which is latest
                if current_q is not None:
                    reduced_list.append(current_q)
                current_ids = ids
                current_dt = dt
                current_q = q
            else:
                # same ids index, so compare datetime and keep later one
                log.debug("id is same")
                if dt > current_dt:
                    #print("update - no append")
                    current_dt = dt
                    current_q = q
        # end loop, so need to append last entry
        if current_q is not None:
            reduced_list.append(current_q)
    return reduced_list

def song_qual_counts_by_part(song,quals):
    log.debug(f"enter qual_counts. song:{song.name} quals:{len(quals)}")
    log.debug(f"song parts:{song.parts}")
    if quals:
        log.debug(f"first qual:{quals[0]}")
    qual_counters = dict()
    part_name_by_id={}
    part_print_order = ['Tenor','Lead','Bari','Bass']
    qual_counters['__part_print_order__'] = part_print_order
    for song_part in song.parts:
        part_name = song_part.part.name
        part_id = song_part.part_id
        part_name_by_id[part_id] = part_name
        if part_name not in part_print_order:
            log.info(f"adding unexpected part {part_name}")
            part_print_order.append(part_name)
            qual_counters[part_name] = 0
    for part in part_print_order:
        qual_counters[part] = 0
    #TODO ideally, should be unexpired...
    for qual in select_quals(quals,song_id=song.id,only_most_recent=True):
        log.debug(f"{qual}")
        part_name = part_name_by_id[qual.part_id]
        qual_counters[part_name] += 1

    return qual_counters

def assign_parts(song, remaining, assigned=[]):
    '''
    assign parts to a setlist_song.  When a part is assigned by a human, it gets sort_order=0.
        when assigned via algorithm, it's assigned sort_order=1.
    '''
    log.debug(f"assign_parts Enter: {song.name} assigned:{assigned} remaining:{len(remaining)}")
    remaining = list(remaining)
    while len(remaining):
        new_assignment = remaining.pop(0)
        if len([x for x in assigned if x.part_id==new_assignment.part_id]):
            # skip if earlier sort_order assignment alread on that part
            if new_assignment.sort_order == 0:
                log.warn(f"multiple assignments with sort_order=0 for {song.name} part_id {new_assignment.part_id}")
            log.debug(f"adding assignment for {new_assignment.part_id} {new_assignment.user_id}")
            continue
        #so add it.
        new_assigned = list(assigned)
        if new_assignment.sort_order < 2000:
            # don't autoassign non-qual entries
            new_assigned.append(new_assignment)
        # TODO eventually use actual parts of song, not just 4
        if len(new_assigned) == 4:
            # full quartet!  assign and return
            log.debug(f"Found Quartet: {new_assigned}")
            for assignment in new_assigned:
                if assignment.sort_order > 1:
                    assignment.update(sort_order=1)
            return True
        # remove newly added singer from remaining assignments for other parts
        if assign_parts(song,[x for x in remaining if x.user_id != new_assignment.user_id],new_assigned):
            return True
    # reached end of list without assigning
    return False

def song_permutations(qual_list,parts_to_fill,debug=False):
    '''
    return a list of tuples (len,used_quals,missing_parts)
    '''
    if debug:
        log.debug(f"enter song_permutations: {[(q.user_id,q.part_id) for q in qual_list]} {[p.part.name for p in parts_to_fill]}")
    permutations = list()
    #return if nothing left to find
    if len(parts_to_fill) == 0:
        permutations.append((0,[],[]))
        return permutations
    if len(qual_list) == 0:
        permutations.append((0,[],parts_to_fill))
        return permutations
    for part in parts_to_fill:
        # make local copies, so we can modify recursively
        sub_parts_to_fill = list(parts_to_fill)
        sub_parts_to_fill.remove(part)
        #qual_list = list(qual_list)
        for part_qual in select_quals(qual_list,part_id=part.part.id):
            if debug:
                log.debug(f"song_permutations: trying {part_qual} ")
            for (p_len,quals,missing_parts) in song_permutations(
                    select_quals(qual_list,exclude_part_id=part.part.id,exclude_user_id=part_qual.user_id),
                    sub_parts_to_fill,debug):
                if debug:
                    log.debug(f"song_permutations: got back {p_len},{quals},{missing_parts}")
                quals.append(part_qual)
                permutations.append((p_len+1,quals,missing_parts))
        else:
            permutations.append((0,[],[part.part.id]))
    return permutations

def qual_strength(qual_list,song):
    '''
    Score a song based on how many valid performance permutations there are.
    If a song cannot be performed it should be scored based on how many parts are required to be added to perform
    Song lists will be sorted by this, so parts needed will be expressed in negative numbers.
    Any performable song will score greater than 0.
    '''
    #limit quals to this song
    qual_list = select_quals(qual_list,song_id=song.id)
    debug = False
    if song.name == "You've Got a Friend in Mezzz":
        debug = True
    permutations = song_permutations(qual_list,song.parts,debug)
    if debug:
        log.debug(permutations)
        log.debug(f"{[q for q in qual_list if q.user_id==7]}")
    max_singers = max([l for (l,qs,ms) in permutations])
    if max_singers == len(song.parts):
        return (len([l for (l,qs,ms) in permutations if l == max_singers]),"")
    max_permutations_missing = [ms for (l, qs, ms) in permutations if l == max_singers]
    missing_part_count = {}
    for part in song.parts:
        missing_part_count[part.part.name] = 0
    for missing_part_list in max_permutations_missing:
        for missing_part in missing_part_list:
            missing_part_count[missing_part.part.name] += 1
    musts = []
    adds = []
    strs = []
    for (count,part) in sorted([(count,part) for (part,count) in missing_part_count.items()],reverse=True):
        if count == len(max_permutations_missing):
            musts.append(part)
        elif count > 0:
            adds.append(part)
    if len(musts):
        strs.append(f"Missing: {'/'.join(musts)}")
    if len(adds):
        strs.append(f"Need: {'/'.join(adds)}")
    return (max_singers - 4," ".join(strs))

def qtet_score(songs,quals,selected_singers):
    # give a score to a (possibly partial) qtet.  Negative if missing parts positive if they can sing a song.
    quals = select_quals(quals,user_ids=[u.id for u in selected_singers],only_most_recent=True)
    sorted_songs = sorted([(qual_strength(quals,s),s) for s in songs],reverse=True)
    ((best_song_strength,missing),song) = sorted_songs[0]
    if best_song_strength > 0:
        best_song_strength = 1
    num_strongest_songs = len([song_strength for ((song_strength,missing),song) in sorted_songs if song_strength >= best_song_strength])
    #return strength of best song and number of songs at best strength
    #  (or performable if strength above 0)
    return (sorted_songs[0][0][0],num_strongest_songs)

def qtet_score_num_songs_median_deviation(songs,quals,selected_singers,median_utility=10):
    # give a score to a (possibly partial) qtet.  Negative if missing parts positive if they can sing a song.
    quals = select_quals(quals,user_ids=[u.id for u in selected_singers],only_most_recent=True)
    #return strength of best song
    sorted_songs = sorted([(qual_strength(quals,s),s) for s in songs],reverse=True)
    singable = [strength for ((strength,missing),song) in sorted_songs if strength >0 ]
    #((strength,missing),song) = sorted_songs[0]
    #print(f"best song - {strength} {song.name} {missing}")
    median_singer_util = sum([s.utility for s in selected_singers][1:-1])/2.0
    deviation = abs(median_singer_util - median_utility)
    return len(singable) * 100 + 50 - deviation
    return sorted_songs[0][0][0]

def singer_relative_order(candidate_singer,selected_singers,qtets):
    # higher rank means you go later
    # make sure people you've already sung with and singers from your org
    #  are deprioritized
    score = candidate_singer.utility
    for s in selected_singers:
        for qtet in qtets:
            if qtet is None:
                continue
            if s in qtet and candidate_singer in qtet:
                score += 15
        if s.org == candidate_singer.org:
            score += 10
    return score

def find_qtet_by_utility(songs,quals,selected_singers,available_singers,previous_qtets):
    """
        expect that available_singers is sorted so just try in order
        return first working qtet
    """
    current_strength,num_songs = qtet_score(songs,quals,selected_singers)
    while current_strength < 0:
        #print(f"qtet current strength = {current_strength}")
        available = [singer for (rank,singer) in sorted([(singer_relative_order(s,selected_singers,previous_qtets),s) for s in available_singers],reverse=False) ]
        #print("available calculated")
        for new_singer in available:
            #print(f"trying {new_singer.name}")
            if new_singer not in selected_singers:
                new_selected_singers = list(selected_singers)
                new_selected_singers.append(new_singer)
                new_strength,new_num_songs = qtet_score(songs,quals,new_selected_singers)
                #print(f"new strength - {new_strength}")
                if new_strength > 0:
                    #print(f"found qtet!  {new_selected_singers}")
                    return new_selected_singers
                if new_strength > current_strength:
                    #print(f"better.  keeping")
                    selected_singers = new_selected_singers
                    current_strength = new_strength
                    available = [singer for (rank,singer) in sorted([(singer_relative_order(s,selected_singers,previous_qtets),s) for s in available_singers],reverse=False) ]
                    #print("break to re-loop")
                    break
        else:
            return None
    return None

def find_qtet_by_most_songs(songs,quals,selected_singers,available_singers,previous_qtets):
    """
        Start with singer with fewest songs.  Try to match qtet that doesn't 
        reduce the number of songs possible.
    """
    # this is the best song strength for a "quartet" of this size
    # basically, we have N parts covered for N singers, so 4-N parts missing.
    min_song_strength = len(selected_singers) - 4
    (current_strength,current_num_good_songs) = qtet_score(songs,quals,selected_singers)
    #print(f"min_song_strength={min_song_strength}, current_strength={current_strength}, good_songs={current_num_good_songs}")
    keep_going = True
    while keep_going:
        #print(f"qtet current strength = {current_strength}")
        available = [singer for (rank,singer) in sorted([(singer_relative_order(s,selected_singers,previous_qtets),s) for s in available_singers],reverse=False) ]
        for new_singer in available:
            #print(f"trying {new_singer.name}")
            if new_singer not in selected_singers:
                new_selected_singers = list(selected_singers)
                new_selected_singers.append(new_singer)
                (new_strength,new_num_songs) = qtet_score(songs,quals,new_selected_singers)
                #print(f"new strength - {new_strength},new_num - {new_num_songs}")
                if new_strength > 0 and new_num_songs >= current_num_good_songs:
                    #print(f"found qtet!  {new_selected_singers}")
                    return new_selected_singers
                if new_strength > current_strength and new_num_songs >= current_num_good_songs:
                    #print(f"better.  keeping")
                    selected_singers = new_selected_singers
                    current_strength = new_strength
                    available = [singer for (rank,singer) in sorted([(singer_relative_order(s,selected_singers,previous_qtets),s) for s in available_singers],reverse=False) ]
                    #print("break to re-loop")
                    break
                #print("nope, move on")
        else:
            #for loop didn't find a quartet, try reducing song number, but for now, punt
            return None
    # didn't find qtet.  fall back to simple method
    #return None
    return find_qtet_by_utility(songs,quals,selected_singers,available_singers,previous_qtets)

find_qtet = find_qtet_by_most_songs

def singer_global_order(singer,qtets=[]):
    # higher rank means you go later
    # make sure singers who have sung the least get to sing
    score = singer.utility
    for qtet in qtets:
        if qtet is None:
            continue
        if singer in qtet:
            score += 50
    return score

def generate_qtets(songs,singers,quals):
    debug = True
    qtet_num = 0
    qtets = []
    all_combinations = list(itertools.combinations(singers,4))
    num_combos = len(all_combinations)
    print(f"qtet combinations: {num_combos}")
    for qtet in all_combinations:
        if debug:
            qtet_num += 1
            if qtet_num % 100 == 0:
                print(f"{qtet_num} out of {num_combos}")

        score = qtet_score(songs,quals,qtet)
        # skip qtets that can't sing anything.
        if score < 0:
            continue
        qtets.append(data_models.Quartet(name="",singers=set(qtet),score=score))
    return qtets

def generate_qtet_combinations(songs,available_singers,quals,already_chosen_qtets=list()):
    if len(already_chosen_qtets) == 0:
        debug = True
        qtet_num = 0
    else:
        debug = False
    num_singers = len(available_singers)
    #print(f"singers: {num_singers}")
    all_combinations = list(itertools.combinations(available_singers,4))
    num_combos = len(all_combinations)
    if debug:
        print(f"qtet combinations: {len(all_combinations)}")
    if num_combos == 0:
        yield already_chosen_qtets
    for qtet in all_combinations:
        if debug:
            qtet_num += 1
            if qtet_num % 1 == 0:
                print(f"{qtet_num} out of {num_combos}")

        score = qtet_score(songs,quals,qtet)
        # print(f"{[x.name for x in qtet]} - {score}")
        # skip qtets that can't sing anything.
        if score <= 0:
            continue
        new_chosen_qtets = list(already_chosen_qtets)
        new_chosen_qtets.append(qtet)
        available_minus_qtet = available_singers - set(qtet)
        yield from generate_qtet_combinations(songs,available_minus_qtet,quals,new_chosen_qtets)

def reduced_qtets(qtets,qtet):
    """
    remove all quartets that share singers with the selected quartet
    """
    reduced = []
    empty = set()
    for q in qtets:
        if q.singers.intersection(qtet.singers) == empty:
            reduced.append(q)
    #print(f"reduced len: {len(reduced)}")
    return reduced

def iter_qtet_sets(qtets,remaining):
    if len(remaining) == 0:
        qset = data_models.QuartetSet(quartets=qtets)
        qset.calculate()
        yield qset
    else:
        for qtet in remaining:
            new_qtets = list(qtets)
            new_qtets.append(qtet)
            yield from iter_qtet_sets(new_qtets,reduced_qtets(remaining,qtet))

def qtet_round_brute_force(songs,quals,singers,previous_qtets):
    all_qtets = sorted(generate_qtets(songs,singers,quals),reverse=True)
    max_num_qtets = int(len(singers)/4)
    print(f"num valid qtets: {len(all_qtets)}")
    print(f"first: {all_qtets[0]}")
    print(f"last: {all_qtets[-1]}")
    print(f"max qtets in round: {max_num_qtets}")
    num = 0
    best_avg = best_min = data_models.QuartetSet(quartets=[])
    for candidate in iter_qtet_sets([],all_qtets):
        num += 1
        if num % 1000000 == 0:
            print(f"{num} candidates")
        if num > 5000000 :
            break
        if candidate.len > best_avg.len or (
                candidate.len == best_avg.len and
                candidate.avg > best_avg.avg
                ):
            best_avg = candidate
            print(f"new best_avg")
            for q in best_avg.quartets:
                print(q)
        if candidate.len > best_min.len or (
                candidate.len == best_min.len and
                candidate.min > best_min.min
                ):
            best_min = candidate
            print(f"new best_min")
            for q in best_min.quartets:
                print(q)
    print(f"best_min")
    for q in best_min.quartets:
        print(q)
    print(f"best_avg")
    for q in best_avg.quartets:
        print(q)
    
    return []
    return sorted(best_avg.quartets)

def qtet_round_by_utility(songs,quals,singers,previous_qtets):
    # previous_qtets is list of previous quartet rounds
    # do some processing here to prefer people with fewer previous qtets
    # and maybe defer leaders/organizers
    #available = [singer for (rank,singer) in sorted([(singer_global_order(s,previous_qtets),s) for s in singers],reverse=False) if singer.override != "Exclude" ]
    #available = sorted_singers(available)
    available = sorted_singers(
            singers,
            sorted_func=functools.partial(
                sorted, key=functools.partial(
                    singer_global_order,qtets=previous_qtets
                )
            )
        )
    qtets = list()
    unused = list()
    while (len(available)>=4):
        singer = available.pop(0)
        #print(f"building qtet around {singer}")
        qtet = find_qtet_by_utility(songs,quals,[singer],available,previous_qtets)
        if qtet is None:
            print(f"{singer.name} cannot be matched.")
            unused.append(singer)
        else:
            print(f"{[s.name for s in qtet]}")
            qtets.append(qtet)
            for qtet_singer in qtet:
                if qtet_singer is not singer:
                    available.remove(qtet_singer)
    unused.extend(available)
    print(f"unused singers: {[x.name for x in unused]}")
    print()
    return qtets

def qtet_round_by_most_songs(songs,quals,singers,previous_qtets):
    # previous_qtets is list of previous quartet rounds
    # do some processing here to prefer people with fewer previous qtets
    # and maybe defer leaders/organizers
    print(f"num previous qtets: {len(previous_qtets)}")
    #available = [singer for (rank,singer) in sorted([(singer_global_order(s,previous_qtets),s) for s in singers],reverse=False) if singer.override != "Exclude" ]
    available = sorted_singers(
            singers,
            sorted_func=functools.partial(
                sorted, key=functools.partial(
                    singer_global_order,qtets=previous_qtets
                )
            )
        )
    qtets = list()
    unused = list()
    while (len(available)>=4):
        singer = available.pop(0)
        print(f"building qtet around {singer}")
        qtet = find_qtet(songs,quals,[singer],available,previous_qtets)
        if qtet is None:
            print(f"{singer.name} cannot be matched.")
            unused.append(singer)
        else:
            print(f"{[s.name for s in qtet]}")
            qtets.append(qtet)
            for qtet_singer in qtet:
                if qtet_singer is not singer:
                    available.remove(qtet_singer)
    unused.extend(available)
    print(f"unused singers: {[x.name for x in unused]}")
    print()
    if len(unused) >= 4:
        #fall back to easier qtet generation for remainder
        qtets.extend(qtet_round_by_utility(songs,quals,unused,previous_qtets))
    return qtets

def sorted_singers(singers,sorted_func=sorted):
    # create new list of singers from existing, 
    # sort based on utility order, modified by override group

    prefers = []
    normals = []
    defers = []
    excludes = []
    for user in sorted_func(singers):
        if user.override == 'Prefer':
            prefers.append(user)
        elif user.override == 'Defer':
            defers.append(user)
        elif user.override == 'Exclude':
            excludes.append(user)
        else:
            normals.append(user)
    singers = []
    singers.extend(prefers)
    singers.extend(normals)
    singers.extend(defers)
    singers.extend(excludes)
    return singers

def name_key(x):
    return x.name

def sorted_by_name(list_to_be_sorted):
    return sorted(list_to_be_sorted,key=name_key)

def seconds_to_colon_separated(duration):
    if duration is None:
        duration = 0
    hours = duration // 3600
    seconds_remaining = duration % 3600
    minutes = seconds_remaining // 60
    seconds = seconds_remaining % 60
    string_bits = []
    if not(minutes or hours):
        return f"{seconds}"
    if not hours:
        return f"{minutes}:{seconds:02}"
    return f"{hours}:{minutes:02}:{seconds:02}"

def colon_separated_to_seconds(duration_string):
    parts = duration_string.split(":")
    total = 0
    while parts:
        total *= 60
        total += int(parts.pop(0))
    return total


