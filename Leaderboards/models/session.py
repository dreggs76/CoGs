from . import APP, MIN_TIME_DELTA, FLOAT_TOLERANCE, MISSING_VALUE, TrueskillSettings

from ..leaderboards import LB_PLAYER_LIST_STYLE, LB_STRUCTURE, player_rankings
from ..trueskill_helpers import TrueSkillHelpers  # Helper functions for TrueSkill, based on "Understanding TrueSkill"

from django.db import models, IntegrityError
from django.db.models import Q
from django.conf import settings
from django.apps import apps
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import localize
from django.utils.timezone import localtime
from django.utils.safestring import mark_safe
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder

from django_model_admin_fields import AdminModel

from django_generic_view_extensions import FIELD_LINK_CLASS
from django_generic_view_extensions.model import TimeZoneMixIn
from django_generic_view_extensions.util import AssertLog
from django_generic_view_extensions.html import NEVER
from django_generic_view_extensions.options import flt, osf
from django_generic_view_extensions.datetime import safe_tz, time_str, make_aware
from django_generic_view_extensions.decorators import property_method
from django_generic_view_extensions.model import field_render, link_target_url, safe_get

from timezone_field import TimeZoneField

from typing import Union
from dateutil import parser
from datetime import datetime, timedelta
from collections import OrderedDict

from math import isclose

import trueskill
import json
import re

from Site.logging import log


class Session(TimeZoneMixIn, AdminModel):
    '''
    The record, with results (Ranks), of a particular Game being played competitively.
    '''
    game = models.ForeignKey('Game', verbose_name='Game', related_name='sessions', null=True, on_delete=models.SET_NULL)  # If the game is deleted keep the session.

    date_time = models.DateTimeField('Time', default=timezone.now)
    date_time_tz = TimeZoneField('Timezone', default=settings.TIME_ZONE, editable=False)

    league = models.ForeignKey('League', verbose_name='League', related_name='sessions', null=True, on_delete=models.SET_NULL)  # If the league is deleted keep the session
    location = models.ForeignKey('Location', verbose_name='Location', related_name='sessions', null=True, on_delete=models.SET_NULL)  # If the location is deleted keep the session

    # The game must support team play if this is true,
    # and conversely, it must support individual play if this false.
    team_play = models.BooleanField('Team Play', default=False)  # By default games are played by individuals, if true, this session was played by teams

    # Foreign Keys that for part of a rich session object
    # ranks = ForeignKey from Rank (one rank per player or team depending on mode)
    # performances = ForeignKey from Performance (one performance per player)

    # A note on session player records:
    #  Players are stored in two distinct places/contexts:
    #    1) In the Performance model - which records each players TrueSkill performance in this session
    #    2) in the Rank model - which records each player or teams rank (placement in the results)
    #
    # The simpler list of players in a sessions is in the Performance model where each player in each session has performance recorded.
    #
    # A less direct record of the players in a  sessionis in the Rank model,
    #     either one player per Rank (in an Individual play session) or one Team per rank (in a Team play session)
    #     This is because ranks are associated with players in individual play mode but teams in Team play mode,
    #     while performance is always tracked by player.

    # TODO: consider if we can filter on properties or specify annotations somehow to filter on them
    filter_options = ['date_time__gt', 'date_time__lt', 'game']
    order_options = ['date_time', 'game', 'league']

    # Two equivalent ways of specifying the related forms that django-generic-view-extensions supports:
    # Am testing the new simpler way now leaving it in place for a while to see if any issues arise.
    # add_related = ["Rank.session", "Performance.session"]  # When adding a session, add the related Rank and Performance objects
    add_related = ["ranks", "performances"]  # When adding a session, add the related Rank and Performance objects

    # Specify which fields to inherit from entry to entry when creating a string of objects
    inherit_fields = ["date_time", "league", "location", "game"]
    inherit_time_delta = timedelta(minutes=90)

    @property
    def date_time_local(self):
        return self.date_time.astimezone(safe_tz(self.date_time_tz))

    @property
    def num_competitors(self) -> int:
        '''
        Returns an integer count of the number of competitors in this game session,
        i.e. number of players in a single-player mode or number of teams in team player mode
        '''
        if self.team_play:
            return len(self.teams)
        else:
            return len(self.players)

    @property
    def str_competitors(self) -> str:
        '''
        Returns a simple string to append to a number which represents the "competitors"
        That is, "team", "teams", "player", or "players" as appropriate. A 1 player
        game is a solo game clearly.
        '''
        n = self.num_competitors
        if self.team_play:
            if n == 1:
                return "team"
            else:
                return "teams"
        else:
            if n == 1:
                return "player"
            else:
                return "players"

    def _ranked_players(self, as_string, link=None) -> Union[dict, str]:
        '''
        Internal factory for ranked_players and str_ranked_players.

        Returns an OrderedDict (keyed on rank) of the players in the session.
        or a CSV string summaring same.

        The value of the dict is a player. The key is "rank.tie_index.team_index"

        :param as_string:  Return a CSV string, else a dict with a compound key
        :param link:    Wrap player names in links according to the provided style.
        '''
        Rank = apps.get_model(APP, "Rank")

        if as_string:
            players = []  # Build list to join later
        else:
            players = OrderedDict()

        ranks = Rank.objects.filter(session=self.id)

        # A quick loop through to check for ties as they will demand some
        # special handling when we collect the list of players into the
        # keyed (by rank) dictionary.
        tie_counts = OrderedDict()
        in_rank_id = OrderedDict()
        for rank in ranks:
            # rank is the rank object, rank.rank is the integer rank (1, 2, 3).
            if rank.rank in tie_counts:
                tie_counts[rank.rank] += 1
                in_rank_id[rank.rank] = 1
            else:
                tie_counts[rank.rank] = 1

        if as_string:
            at_rank = None  # For keeping track of a rank during tie (which see multple rank objects at the same ranking)
            team_separator = "+"
            tie_separator = "/"

        for rank in ranks:
            # rank is the rank object, rank.rank is the integer rank (1, 2, 3).
            if self.team_play:
                if as_string and rank.rank != at_rank and len(players) > 0 and isinstance(players[-1], list):  # The tie-list is complete so we can stringify it
                    tie_members = players.pop()
                    players.append(tie_separator.join(tie_members))
                    at_rank = None

                if tie_counts[rank.rank] > 1:
                    if as_string:
                        team_members = team_separator.join([field_render(player.name_nickname, link_target_url(player, link)) for player in rank.players])

                        if rank.rank == at_rank:
                            players[-1].append(team_members)
                        else:
                            players.append([team_members])
                            at_rank = rank.rank
                    else:
                        for pid, player in enumerate(rank.players):
                            players[f"{rank.rank}.{in_rank_id[rank.rank]}.{pid}"] = player
                        in_rank_id[rank.rank] += 1
                else:
                    if as_string:
                        team_members = team_separator.join([field_render(player.name_nickname, link_target_url(player, link)) for player in rank.players])
                        players.append(team_members)
                    else:
                        pid = 1
                        for player in rank.players:
                            players["{}.{}".format(rank.rank, pid)] = player
                            pid += 1
            else:
                # The players can be listed (indexed) in rank order.
                # When there are multiple players at the same rank (ties)
                # We use a decimal format of rank.person to ensure that
                # the sorting remains more or less sensible.
                if as_string and rank.rank != at_rank and len(players) > 0 and isinstance(players[-1], list):  # The tie-list is complete so we can stringify it
                    tie_members = players.pop()
                    players.append(tie_separator.join(tie_members))
                    at_rank = None

                if tie_counts[rank.rank] > 1:  # There is a tie!
                    if as_string:
                        name = field_render(rank.player.name_nickname, link_target_url(rank.player, link))
                        if rank.rank == at_rank:
                            players[-1].append(name)
                        else:
                            players.append([name])
                            at_rank = rank.rank
                    else:
                        players[f"{rank.rank}.{in_rank_id[rank.rank]}"] = rank.player
                        in_rank_id[rank.rank] += 1
                else:  # There is no tie
                    if as_string:
                        name = field_render(rank.player.name_nickname, link_target_url(rank.player, link))
                        players.append(name)
                    else:
                        players[f"{rank.rank}"] = rank.player

        if as_string and isinstance(players[-1], list):  # The tie-list is complete so we can stringify it
            tie_members = players.pop()
            players.append(tie_separator.join(tie_members))

        return ", ".join(players) if as_string else players

    @property
    def ranked_players(self) -> dict:
        '''
        Returns a dict of players with the key storing rank information in form:

        rank.tie_index.team_index
        '''
        return self._ranked_players(False)

    @property_method
    def str_ranked_players(self, link=flt.internal) -> str:
        '''
        Returns a list of players 9as a CSV string) in rank order (with team members and tied annotated)
        '''
        return self._ranked_players(True, link)

    @property
    def players(self) -> set:
        '''
        Returns an unordered set of the players in the session, with no guaranteed
        order. Useful for traversing a list of all players in a session
        irrespective of the structure of teams or otherwise.

        '''
        Performance = apps.get_model(APP, "Performance")

        players = set()
        performances = Performance.objects.filter(session=self.pk)

        for performance in performances:
            players.add(performance.player)

        return players

    @property
    def ranked_teams(self) -> dict:
        '''
        Returns an OrderedDict (keyed on rank) of the teams in the session.
        The value is a list of players (in team play sessions)
        Returns an empty dictionary for Individual play sessions

        Note ties have the same rank, so the key has a .index appended,
        to form a unique key. Only the key digits up to the . represent
        the true rank, the full key permits sorting and inique storage
        in a dictionary.
        '''
        Rank = apps.get_model(APP, "Rank")

        teams = OrderedDict()
        if self.team_play:
            ranks = Rank.objects.filter(session=self.id)

            # a quick loop through to check for ties as they will demand some
            # special handling when we collect the list of players into the
            # keyed (by rank) dictionary.
            rank_counts = OrderedDict()
            rank_id = OrderedDict()
            for rank in ranks:
                # rank is the rank object, rank.rank is the integer rank (1, 2, 3).
                if rank.rank in rank_counts:
                    rank_counts[rank.rank] += 1
                    rank_id[rank.rank] = 1
                else:
                    rank_counts[rank.rank] = 1

            for rank in ranks:
                # The players can be listed (indexed) in rank order.
                # When there are multiple players at the same rank (ties)
                # We use a decimal format of rank.person to ensure that
                # the sorting remains more or less sensible.
                if rank_counts[rank.rank] > 1:
                    teams["{}.{}".format(rank.rank, rank_id[rank.rank])] = rank.team
                    rank_id[rank.rank] += 1
                else:
                    teams["{}".format(rank.rank)] = rank.team
        return teams

    @property
    def teams(self) -> set:
        '''
        Returns an unordered set of the teams in the session, with no guaranteed
        order. Useful for traversing a list of all teams in a session
        irrespective of the ranking.

        '''
        Rank = apps.get_model(APP, "Rank")

        teams = set()

        if self.team_play:
            ranks = Rank.objects.filter(session=self.pk)

            for rank in ranks:
                teams.add(rank.team)

            return teams
        else:
            return None

    @property
    def victors(self) -> set:
        '''
        Returns the victors, a set of players or teams. Plural because of possible draws.
        '''
        Rank = apps.get_model(APP, "Rank")

        victors = set()
        ranks = Rank.objects.filter(session=self.id)

        for rank in ranks:
            # rank is the rank object, rank.rank is the integer rank (1, 2, 3).
            if self.team_play:
                if rank.rank == 1:
                    victors.add(rank.team)
            else:
                if rank.rank == 1:
                    victors.add(rank.player)
        return victors

    @property
    def trueskill_impacts(self) -> dict:
        '''
        Returns the recorded trueskill impacts of this session.
        Does not (re)calculate them, reads the recorded Performance records
        '''
        players_left = self.players

        impact = OrderedDict()
        for performance in self.performances.all():
            if performance.player in players_left:
                players_left.discard(performance.player)
            else:
                raise IntegrityError("Integrity error: Session has a player performance without a matching rank. Session id: {}, Performance id: {}".format(self.id, performance.id))

            impact[performance.player] = OrderedDict([
                ('plays', performance.play_number),
                ('victories', performance.victory_count),
                ('last_play', performance.session.date_time),
                ('last_victory', performance.session.date_time if performance.is_victory else performance.rating.last_victory),
                ('delta', OrderedDict([
                            ('mu', performance.trueskill_mu_after - performance.trueskill_mu_before),
                            ('sigma', performance.trueskill_sigma_after - performance.trueskill_sigma_before),
                            ('eta', performance.trueskill_eta_after - performance.trueskill_eta_before)
                            ])),
                ('after', OrderedDict([
                            ('mu', performance.trueskill_mu_after),
                            ('sigma', performance.trueskill_sigma_after),
                            ('eta', performance.trueskill_eta_after)
                            ])),
                ('before', OrderedDict([
                            ('mu', performance.trueskill_mu_before),
                            ('sigma', performance.trueskill_sigma_before),
                            ('eta', performance.trueskill_eta_before)
                            ]))
            ])

        assert len(players_left) == 0, "Integrity error: Session has ranked players without a matching performance. Session id: {}, Players: {}".format(self.id, players_left)

        return impact

    @property
    def trueskill_code(self) -> str:
        '''
        A debugging property that prints python code that will replicate this trueskill calculation
        So that this specific trueksill calculation might be diagnosed and debugged in isolation.
        '''
        TSS = TrueskillSettings()
        OldRatingGroups, Weights, Ranking = self.build_trueskill_data()

        code = []
        code.append("<pre>#!/usr/bin/python3")
        code.append("import trueskill")
        code.append("mu0 = {}".format(TSS.mu0))
        code.append("sigma0 = {}".format(TSS.sigma0))
        code.append("delta = {}".format(TSS.delta))
        code.append("beta = {}".format(self.game.trueskill_beta))
        code.append("tau = {}".format(self.game.trueskill_tau))
        code.append("p = {}".format(self.game.trueskill_p))
        code.append("TS = trueskill.TrueSkill(mu=mu0, sigma=sigma0, beta=beta, tau=tau, draw_probability=p)")
        code.append("oldRGs = {}".format(str(OldRatingGroups)))
        code.append("Weights = {}".format(str(Weights)))
        code.append("Ranking = {}".format(str(Ranking)))
        code.append("newRGs = TS.rate(oldRGs, Ranking, Weights, delta)")
        code.append("print(newRGs)")
        code.append("</pre>")
        return "\n".join(code)

    def _get_future_sessions(self, sessions_so_far):
        '''
        Internal support for the future_session property, this is a recursive method
        that takes a list of sessions found so far so as to avoid duplicating any
        sessions in the search.

        Why recursion? Because future sessions is an influence tree where each
        node branches a multiple of times. Consider agame that involves four
        playes P1, P2, P3, P4. We can get all future session in this game that
        any of these four players played in with a simple query. BUT in all
        those sessions they maybe (probably) played with otehr people. So say
        theres a futre session between P1, P2, P5 and P6? Well we need to find
        all the future sessions in this game that involve P1, P2, P5 or P6! So
        essentially future sessions fromt he simpe query can drag in new players
        which form new influence trees.

        The premise in building this tree is that it is is far more efficient than
        reacuaulating Trueskill ratings on them all. Thus finding a count helps us
        estimate the cost of performing a rebuild.

        sessions_so_far: A list of sessions found so far, that is augmented and returned
        '''
        # We want session in the future only of course
        dfilter = Q(date_time__gt=self.date_time)

        # We want only sessions for this sessions game
        gfilter = Q(game=self.game)

        # For each player we find all future sessions playing this game
        pfilter = Q(performances__player__in=self.players)

        # Combine the filters
        filters = dfilter & gfilter & pfilter

        # Get the list of PKs to exclude
        exclude_pks = list(map(lambda s: s.pk, sessions_so_far))

        new_future_sessions = Session.objects.filter(filters).exclude(pk__in=exclude_pks).distinct().order_by('date_time')

        if new_future_sessions.count() > 0:
            # augment sessions_so far
            sessions_so_far += list(new_future_sessions)

            # The new future sessions may involve new players which
            # requires that we scan them for new future sessions too
            for session in new_future_sessions:
                new_sessions_so_far = session._get_future_sessions(sessions_so_far)

                if len(new_sessions_so_far) > len(sessions_so_far):
                    sessions_so_far = sorted(new_sessions_so_far, key=lambda s: s.date_time)

        return sessions_so_far

    @property
    def future_sessions(self) -> list:
        '''
        Returns the sessions ordered by date_time that are in the future relative to this session
        that involve this game and any of the players in this session, or players in those sessions.

        Namely every session that needs to be re-evaluated because this one has been inserted before
        it, or edited in some way.
        '''
        return self._get_future_sessions([])

    @property
    def link_internal(self) -> str:
        return reverse('view', kwargs={"model":self._meta.model.__name__, "pk": self.pk})

    @property
    def actual_ranking(self) -> tuple:
        '''
        Returns a tuple of rankers (Players, teams, or tuple of same for ties) in the actual
        recorded order as the first element in a tuple.

        The second is the probability associated with that observation based on skills of
        players in the session.
        '''
        g = self.game
        ts = TrueSkillHelpers(tau=g.trueskill_tau, beta=g.trueskill_beta, p=g.trueskill_p)
        return ts.Actual_ranking(self)

    @property
    def predicted_ranking(self) -> tuple:
        '''
        Returns a tuple of rankers (Players, teams, or tuple of same for ties) in the predicted
        order (based on skills enterig the session) as the first element in a tuple.

        The second is the probability associated with that prediction based on skills of
        players in the session.
        '''
        g = self.game
        ts = TrueSkillHelpers(tau=g.trueskill_tau, beta=g.trueskill_beta, p=g.trueskill_p)
        return ts.Predicted_ranking(self)

    @property
    def predicted_ranking_after(self) -> tuple:
        '''
        Returns a tuple of rankers (Players, teams, or tuple of same for ties) in the predicted
        order (using skills updated on the basis of the actual results) as the first element in a tuple.

        The second is the probability associated with that prediction based on skills of
        players in the session.
        '''
        g = self.game
        ts = TrueSkillHelpers(tau=g.trueskill_tau, beta=g.trueskill_beta, p=g.trueskill_p)
        return ts.Predicted_ranking(self, after=True)

    @property
    def relationships(self) -> set:
        '''
        Returns a list of tuples containing player or team pairs representing
        each ranker (contestant) relationship in the game.

        Tuples always ordered (victor, loser) except on draws in which case arbitrary.
        '''
        ranks = self.ranks.all()
        relationships = set()
        # Not the most efficient walk but a single game has a comparatively small
        # number of rankers (players or teams ranking) and efficiency not a drama
        # More efficient would be not rewalk walked ground (i.e second loop only has
        # to go from outer loop index up to end.
        for rank1 in ranks:
            for rank2 in ranks:
                if rank1 != rank2:
                    relationship = (rank1.ranker, rank2.ranker) if rank1.rank < rank2.rank else (rank2.ranker, rank1.ranker)
                    if not relationship in relationships:
                        relationships.add(relationship)

        return relationships

    @property
    def player_relationships(self) -> set:
        '''
        Returns a list of tuples containing player pairs representing each player relationship
        in the game. Sale as self.relationships() in individual play mode, differs onl in team
        mode in that it find all the player relationships and ignores team relationships.

        Tuples always ordered (victor, loser) except on draws in which case arbitrary.
        '''
        performances = self.performances.all()
        relationships = set()
        # Not the most efficient walk but a single game has a comparatively small
        # number of rankers (players or teams ranking) and efficiency not a drama
        # More efficient would be not rewalk walked ground (i.e second loop only has
        # to go from outer loop index up to end.
        for performance1 in performances:
            for performance2 in performances:
                # Only need relationships where 1 beats 2 or there's a draw
                if performance1.rank.rank <= performance2.rank.rank and performance1.player != performance2.player:
                    relationship = (performance1.player, performance2.player)
                    back_relationship = (performance2.player, performance1.player)
                    if not back_relationship in relationships:
                        relationships.add(relationship)

        return relationships

    def _prediction_quality(self, after=False) -> int:
        '''
        Returns a measure of the prediction quality that TrueSkill rankings
        provided. A number from 0 to 1. 0 being got it all wrong, 1 being got
        it all right.
        '''

        def dictify(ordered_rankers):
            '''
            Given a list of rankers in order will return a dictionary keyed on ranker with rank based on that order.
            '''
            rank_dict = {}
            r = 1
            for rank, rankers in enumerate(ordered_rankers):
                # Get a list of tied rankers (list of 1 if no tie) so we can handle ti as a list here-on in
                if isinstance(rankers, (list, tuple)):
                    tied_rankers = rankers
                else:
                    tied_rankers = [rankers]

                for ranker in tied_rankers:
                    rank_dict[ranker] = rank

            return rank_dict

        actual_rank = dictify(self.actual_ranking[0])
        predicted_rank = dictify(self.predicted_ranking_after[0]) if after else dictify(self.predicted_ranking[0])
        total = 0
        right = 0
        for relationship in self.relationships:
            ranker1 = relationship[0]
            ranker2 = relationship[1]
            real_result = actual_rank[ranker1] < actual_rank[ranker2]
            pred_result = predicted_rank[ranker1] < predicted_rank[ranker2]
            total += 1
            if pred_result == real_result:
                right += 1

        return right / total if total > 0 else 0

    @property
    def prediction_quality(self) -> int:
        return self._prediction_quality()

    @property
    def prediction_quality_after(self) -> int:
        return self._prediction_quality(True)

    @property
    def inspector(self) -> str:
        '''
        Returns a safe HTML string reporting the structure of a session for prurposes
        of rapid and easy debugging of any database integrity issues. Many other
        properties and methods make assumptions about session integrity and if these fail
        they bomb. The aim here is that this is robust and just reports the database
        objects related and their basic properties with PKs in a nice HTML div that
        can be popped onto any page or on a spearate "inspector" page if desired.
        '''
        Team = apps.get_model(APP, "Team")
        Rank = apps.get_model(APP, "Rank")

        # A TootlTip Format string
        ttf = "<div class='tooltip'>{}<span class='tooltiptext'>{}</span></div>"

        html = "<div id='session_inspector' class='inspector'>"
        html += "<table>"
        html += f"<tr><th>pk:</th><td>{self.pk}</td></tr>"
        html += f"<tr><th>date_time:</th><td>{self.date_time}</td></tr>"
        html += f"<tr><th>league:</th><td>{self.league.pk}: {self.league.name}</td></tr>"
        html += f"<tr><th>location:</th><td>{self.location.pk}: {self.location.name}</td></tr>"
        html += f"<tr><th>game:</th><td>{self.game.pk}: {self.game.name}</td></tr>"
        html += f"<tr><th>team_play:</th><td>{self.team_play}</td></tr>"

        pid = ttf.format("pid", "Performance ID - the primary key of a Performance object")
        rid = ttf.format("rid", "Rank ID - the primary key of a Rank object")
        tid = ttf.format("tid", "Ream ID - the primary key of a Team object")

        html += "<tr><th>{}</th><td><table>".format(ttf.format("Integrity:", "Every player in the game must have an associated performance, rank and if relevant, team object"))

        for performance in self.performances.all():
            html += "<tr>"
            html += f"<th>player:</th><td>{performance.player.pk}</td><td>{performance.player.full_name}</td>"
            html += f"<th>{pid}:</th><td>{performance.pk}</td>"

            rank = None
            team = None
            if self.team_play:
                ranks = Rank.objects.filter(session=self)
                for r in ranks:
                    if not r.team is None:  # Play it safe in case of database integrity issue
                        try:
                            t = Team.objects.get(pk=r.team.pk)
                        except Team.DoesNotExist:
                            t = None

                        players = t.players.all() if not t is None else []

                        if performance.player in players:
                            rank = r.pk
                            team = t.pk
            else:
                try:
                    rank = Rank.objects.get(session=self, player=performance.player).pk
                    html += f"<th>{rid}:</th><td>{rank}</td>"
                except Rank.DoesNotExist:
                    rank = None
                    html += f"<th>{rid}:</th><td>{rank}</td>"
                except Rank.MultipleObjectsReturned:
                    ranks = Rank.objects.filter(session=self, player=performance.player)
                    html += f"<th>{rid}:</th><td>{[rank.pk for rank in ranks]}</td>"

            html += f"<th>{tid}:</th><td>{team}</td>" if self.team_play else ""
            html += "</tr>"
        html += "</table></td></tr>"

        html += "<tr><th>ranks:</th><td><ol start=0>"
        for rank in self.ranks.all():
            html += "<li><table>"
            html += f"<tr><th>pk:</th><td>{rank.pk}</td></tr>"
            html += f"<tr><th>rank:</th><td>{rank.rank}</td></tr>"
            html += f"<tr><th>player:</th><td>{rank.player.pk if rank.player else None}</td><td>{rank.player.full_name if rank.player else None}</td></tr>"
            html += f"<tr><th>team:</th><td>{rank.team.pk if rank.team else None}</td><td>{rank.team.name if rank.team else ''}</td></tr>"
            if (rank.team):
                for player in rank.team.players.all():
                    html += f"<tr><th></th><td>{player.pk}</td><td>{player.full_name}</td></tr>"
            html += "</table></li>"
        html += "</ol></td></tr>"

        html += "<tr><th>performances:</th><td><ol start=0>"
        for performance in self.performances.all():
            html += "<li><table>"
            html += f"<tr><th>pk:</th><td>{performance.pk}</td></tr>"
            html += f"<tr><th>player:</th><td>{performance.player.pk}</td><td>{performance.player.full_name}</td></tr>"
            html += f"<tr><th>weight:</th><td>{performance.partial_play_weighting}</td></tr>"
            html += f"<tr><th>play_number:</th><td>{performance.play_number}</td>"
            html += f"<th>victory_count:</th><td>{performance.victory_count}</td></tr>"
            html += f"<tr><th>mu_before:</th><td>{performance.trueskill_mu_before}</td>"
            html += f"<th>mu_after:</th><td>{performance.trueskill_mu_after}</td></tr>"
            html += f"<tr><th>sigma_before:</th><td>{performance.trueskill_sigma_before}</td>"
            html += f"<th>sigma_after:</th><td>{performance.trueskill_sigma_after}</td></tr>"
            html += f"<tr><th>eta_before:</th><td>{performance.trueskill_eta_before}</td>"
            html += f"<th>eta_after:</th><td>{performance.trueskill_eta_after}</td></tr>"
            html += "</table></li>"
        html += "</ol></td></tr>"
        html += "</table>"
        html += "</div>"

        return html

    def leaderboard(self, leagues=[], asat=None, names="nick", style=LB_PLAYER_LIST_STYLE.rich, data=None):
        '''
        Returns the leaderboard for this session's game as at a given time, in the form of
        LB_STRUCTURE.player_list

        Primarily to support self.leaderboard_before() and self.leaderboard_after()

        This cannot be easily session_wrapped becasue of the as_at argument that defers to
        Game.leaderboard which has no session for context.

        :param leagues:      Game.leaderboards argument passed through
        :param asat:         Game.leaderboard argument passed through
        :param names:        Game.leaderboard argument passed through
        :param style:        Game.leaderboard argument passed through
        :param data:         Game.leaderboard argument passed through
        '''
        if not asat:
            asat = self.date_time

        return self.game.leaderboard(leagues, asat, names, style, data)

    @property_method
    def leaderboard_before(self, style=LB_PLAYER_LIST_STYLE.rich, wrap=False) -> tuple:
        '''
        Returns the leaderboard as it was immediately before this session, in the form of
        LB_STRUCTURE.player_list

        :param style: a LB_PLAYER_LIST_STYLE to use.
        :param wrap: If true puts the previous sessions session wrapper around the leaderboard.
        '''
        session = self.previous_session()
        player_list = self.leaderboard(asat=self.date_time - MIN_TIME_DELTA, style=style)

        if player_list:
            if wrap:
                leaderboard = session.wrapped_leaderboard(player_list)
            else:
                leaderboard = player_list
        else:
            leaderboard = None

        return leaderboard

    @property_method
    def leaderboard_after(self, style=LB_PLAYER_LIST_STYLE.rich, wrap=False) -> tuple:
        '''
        Returns the leaderboard as it was immediately after this session, in the form of
        LB_STRUCTURE.player_list

        :param style: a LB_PLAYER_LIST_STYLE to use.
        :param wrap: If true puts this sessions session wrapper around the leaderboard.
        '''
        session = self
        player_list = self.leaderboard(asat=self.date_time, style=style)

        if wrap:
            leaderboard = session.wrapped_leaderboard(player_list)
        else:
            leaderboard = player_list

        return leaderboard

    @property_method
    def wrapped_leaderboard(self, leaderboard=None, leagues=[], asat=None, names="nick", style=LB_PLAYER_LIST_STYLE.simple, data=None) -> tuple:
        '''
        Given a leaderboard with structure
            LB_STRUCTURE.player_list
        will wrap it in this session's data to return a board with structure
            LB_STRUCTURE.session_wrapped_player_list

        A session wrapper contains:
            session.pk,
            session.date_time (in local time),
            session.game.play_counts()['total'],
            session.game.play_counts()['sessions'],
            session.players() (as a list of pks),
            session.leaderboard_header(),
            session.leaderboard_analysis(),
            session.leaderboard_analysis_after(),
            game.leaderboard(asat)  # leaderboard after this game sessions
            game.leaderboard()      # current (latest) leaderboard for this game (if needed for diagnostics or otherwise)

        :param leaderboard:
        :param leagues:      self.leaderboard argument passed through
        :param asat:         self.leaderboard argument passed through
        :param names:        self.leaderboard argument passed through
        :param style:        self.leaderboard argument passed through
        :param data:         self.leaderboard argument passed through
        '''
        if leaderboard is None:
            leaderboard = self.leaderboard(leagues, asat, names, style, data)

        if asat is None:
            asat = self.date_time

        # Get the play couns as at asat
        counts = self.game.play_counts(asat=asat)

        # TODO: Respect the style
        # This is .rich
        # .data should be minimlist to enable reconstruction
        # Consider how others would be. Problem is the analysis blocks are rich and not great for storing in ChangeLogs.
        # It is they that probably need to respect the rich vs simple vs data style.
        # none might suggest no wrapping?
        # data be minimalist
        # rating and ratings could mape to simple
        # rich is the current default

        # Build the snapshot tuple
        return (self.pk,
                localize(localtime(self.date_time)),
                counts['total'],
                counts['sessions'],
                [p.pk for p in self.players],
                self.leaderboard_header(),
                self.leaderboard_analysis(),
                self.leaderboard_analysis_after(),
                leaderboard)

    @property
    def leaderboard_snapshot(self) -> tuple:
        '''
        Prepares a leaderboard snapshot for passing to a view for rendering.

        The structure is decribed as LB_STRUCTURE.session_wrapped_player_list

        That is: the leaderboard in this game as it stood just after this session was played.

        Such snapshots are often delivered inside a game wrapper.

        This is differs from wrapped_leaderboard only in that it is a shorthand for a
        common use case and includes a diagnostic snapshot (the latest game board) if
        this is the last session in this game and the latest board is not the same.

        To clarify, the snapshot builds the board from the latest performances of
        all players at the time this session was played, while the latest game board
        uses the ratings as they stand. A difference suggests the ratings don't reflect
        the performances and a data integrity issue.
        '''

        # Get the leaderboard asat the time of this session.
        # That includes the performances of this session and
        # hence the impact of this session.
        #
        # We provide an annotated version which supplies us with
        # the information needed for player filtering and rendering,
        # the leaderboard returned is complete (no league filter applied,
        # or name rendering options supplied).
        #
        # It will be up to the view to filter players as desired and
        # select the name format at render time.

        if settings.DEBUG:
            log.debug(f"\t\t\tBuilding leaderboard snapshot for {self.pk}")

        # Build the snapshot tuple (session wrapped leaderboard)
        leaderboard = self.leaderboard_after(style=LB_PLAYER_LIST_STYLE.rich)  # returns LB_STRUCTURE.player_list
        snapshot = self.wrapped_leaderboard(leaderboard)

        return snapshot

    @property_method
    def leaderboard_impact(self, style=LB_PLAYER_LIST_STYLE.rich) -> tuple:
        '''
        Returns a game_wrapped_session_wrapped pair of player_lists representing the leaderboard
        for this session game as it stood before the session was played, and after it was played.
        '''
        before = self.leaderboard_before(style=style, wrap=True)  # session wrapped
        after = self.leaderboard_after(style=style, wrap=True)  # session wrapped

        # Append the latest only for diagnostics if it's expected to be same and isn't! So we have the two to compare diagnostically!
        # if this is the latest sesion, then the latest leaderbouard shoudl be the same as this session's snapshot!
        if self.is_latest:
            player_list = self.game.leaderboard(style=style)  # returns LB_STRUCTURE.player_list

            # Session wrap it for consistency of structure (even though teh session wrapper is faux, meaning
            # the latest board for this game is not from this session if it's not the same as this session after board)
            latest = self.wrapped_leaderboard(player_list)

            # TODO: Check that this comparison works. It's  aguess for now. probably does NOT WORK
            include_latest = not after == latest

            if include_latest:
                # TODO: For now just a diagnostic check but what should be do in general?
                # Report it on the impact view? Log it? Fix it?
                # breakpoint()
                pass
        else:
            include_latest = False

        # Build the tuple of session wrapped boards
        sw_boards = [after]
        if before: sw_boards.append(before)
        if include_latest: sw_boards.append(latest)

        return self.game.wrapped_leaderboard(sw_boards, snap=True, has_baseline=include_latest)

    @property
    def player_ranking_impact(self) -> dict:
        '''
        Returns a dict keyed on player (whose ratings were affected by by this rebuild) whose value is their rank change on the leaderboard.
        '''
        Player = apps.get_model(APP, "Player")

        before = self.leaderboard_before(style=LB_PLAYER_LIST_STYLE.data, wrap=False)  # NOT session wrapped
        after = self.leaderboard_after(style=LB_PLAYER_LIST_STYLE.data, wrap=False)  # NOT session wrapped

        deltas = {}
        old = player_rankings(before, structure=LB_STRUCTURE.player_list) if before else None
        new = player_rankings(after, structure=LB_STRUCTURE.player_list)

        for p in new:
            _old = old.get(p, len(new)) if old else len(new)
            if not new[p] == _old:
                delta = new[p] - _old
                P = safe_get(Player, p)
                deltas[P] = delta

        return deltas

    def _html_rankers_ol(self, ordered_rankers, use_rank, expected_performance, name_style, ol_style="margin-left: 8ch;"):
        '''
        Internal OL factory for list of rankers on a session.

        :param ordered_ranks:           Rank objects in order we'd like them listed.
        :param use_rank:                Use Rank.rank to permit ties, else use the row number
        :param expected_performance:    Name of Rank property that supplies a Predicted Performance summary
        :param name_style:              The style in which to render names
        :param ol_style:                A style to apply to the OL if any
        '''
        Player = apps.get_model(APP, "Player")
        Team = apps.get_model(APP, "Team")

        data = []  # A list of (PK, BGGname) tuples as data for a template view to build links to BGG if desired.
        if ol_style:
            detail = f'<OL style="{ol_style}">'
        else:
            detail = '<OL>'

        rankers = OrderedDict()
        for row, ranker in enumerate(ordered_rankers):
            if isinstance(ranker, (list, tuple)):
                tied_rankers = ranker
            else:
                tied_rankers = [ranker]

            tied_rankers_html = []
            for r in tied_rankers:
                if isinstance(r, Team):
                    # Teams we can render with the default format
                    ranker = field_render(r, flt.template)
                    data.append((r.pk, None))  # No BGGname for a team
                elif isinstance(r, Player):
                    # Render the field first as a template which has:
                    # {Player.PK} in place of the player's name, and a
                    # {link.klass.model.pk}  .. {link_end} wrapper around anything that needs a link
                    ranker = field_render(r , flt.template, osf.template)

                    # Replace the player name template item with the formatted name of the player
                    ranker = re.sub(fr'{{Player\.{r.pk}}}', r.name(name_style), ranker)

                    # Add a (PK, BGGid) tuple to the data list that provides a PK to BGGid map for a the leaderboard template view
                    PK = r.pk
                    BGG = None if (r.BGGname is None or len(r.BGGname) == 0 or r.BGGname.isspace()) else r.BGGname
                    data.append((PK, BGG))

                # Add expected performance to the ranker string if requested
                eperf = ""
                if not expected_performance is None:
                    perf = getattr(r, expected_performance, None)  # (mu, sigma)
                    if not perf is None:
                        eperf = perf[0]  # mu

                if eperf:
                    tip = "<span class='tooltiptext' style='width: 600%;'>Expected performance (teeth)</span>"
                    ranker += f" (<div class='tooltip'>{eperf:.1f}{tip}</div>)"

                tied_rankers_html.append(ranker)

            conjuntion = "<BR>" if len(tied_rankers_html) > 3 else ", "
            rankers[row] = conjuntion.join(tied_rankers_html)

        for row, tied_rankers in rankers.items():
            detail += f'<LI value={row+1}>{tied_rankers}</LI>'

        detail += '</OL>'

        return (detail, data)

    def leaderboard_header(self, name_style="flexi"):
        '''
        Returns a HTML header that can be used on leaderboards.

        It includes the ranked list of performers in that session.

        This comes in two parts, a template, and ancillary data.

        The template is HTML with placeholders for the ancillary data.

        This permits a leaderboard view to render the template altering how
        the template is rendered.  The ancillary data is for now just the
        pk and BGG name of the ranker in that session which allows the
        template to link names to this site or to BGG as it desires.

        :param name_style: Must be supplied
        '''
        (ordered_rankers, probability) = self.actual_ranking

        detail = f"<b>Results after: <a href='{link_target_url(self)}' class='{FIELD_LINK_CLASS}'>{time_str(self.date_time)}</a></b><br><br>"

        (ol, data) = self._html_rankers_ol(ordered_rankers, True, None, name_style)

        detail += ol

        detail += f"This result was deemed {probability:0.1%} likely."

        return (detail, data)

    def leaderboard_analysis(self, name_style="flexi"):
        '''
        Returns a HTML header that can be used on leaderboards.

        It includes an analysis of the session.

        This comes in two parts, a template, and ancillary data.

        The template is HTML with placeholders for the ancillary data.

        This permits a leaderboard view to render the template altering how
        the template is rendered.  The ancillary data is for now just the
        pk and BGG name of the ranker in that session which allows the
        template to link names to this site or to BGG as it desires.

        Format is as follows:

        1) An ordered list of players as the prediction
        2) A confidence in the prediction (a measure of probability)
        3) A quality measure of that prediction

        :param name_style: Must be supplied
        '''
        (ordered_rankers, confidence) = self.predicted_ranking
        quality = self.prediction_quality

        tip_sure = "<span class='tooltiptext' style='width: 500%;'>Given the expected performance of players, the probability that this predicted ranking would happen.</span>"
        tip_accu = "<span class='tooltiptext' style='width: 300%;'>Compared with the actual result, what percentage of relationships panned out as expected performances predicted.</span>"
        detail = f"Predicted ranking <b>before</b> this session,<br><div class='tooltip'>{confidence:.0%} sure{tip_sure}</div>, <div class='tooltip'>{quality:.0%} accurate{tip_accu}</div>: <br><br>"
        (ol, data) = self._html_rankers_ol(ordered_rankers, False, "performance", name_style)

        detail += ol

        return (mark_safe(detail), data)

    def leaderboard_analysis_after(self, name_style="flexi"):
        '''
        Returns a HTML header that can be used on leaderboards.

        It includes an analysis of the session updates.

        This comes in two parts, a templates, and ancillary data.

        The template is HTML with placeholders for the ancillary data.

        This permits a leaderboard view to render the template altering how
        the template is rendered.  The ancillary data is for now just the
        pk and BGG name of the ranker in that session which allows the
        template to link names to this site or to BGG as it desires.

        Format is as follows:

        1) An ordered list of players as a the prediction
        2) A confidence in the prediction (some measure of probability)
        3) A quality measure of that prediction

        :param name_style: Must be supplied
        '''
        (ordered_rankers, confidence) = self.predicted_ranking_after
        quality = self.prediction_quality_after

        tip_sure = "<span class='tooltiptext' style='width: 500%;'>Given the expected performance of players, the probability that this predicted ranking would happen.</span>"
        tip_accu = "<span class='tooltiptext' style='width: 300%;'>Compared with the actual result, what percentage of relationships panned out as expected performances predicted.</span>"
        detail = f"Predicted ranking <b>after</b> this session,<br><div class='tooltip'>{confidence:.0%} sure{tip_sure}</div>, <div class='tooltip'>{quality:.0%} accurate{tip_accu}</div>: <br><br>"
        (ol, data) = self._html_rankers_ol(ordered_rankers, False, "performance_after", name_style)
        detail += ol

        return (mark_safe(detail), data)

    def leaderboard_analysis_current(self, name_style="flexi"):
        pass
        # TODO: Return the probability of the current ranking. So it's not part of
        # theResults box above and we can rearrgae the optiiopns maybe to have TrueSkill section
        # with Result Analsyis, Prediction Prior, Prediction Post.

    def previous_sessions(self, player=None):
        '''
        Returns all the previous sessions that the nominate player played this game in.

        Always includes the current session as the first item (previous_sessions[0]).

        :param player: A Player object. Optional, all previous this game was played in if not provided.
        '''
        # TODO: Test thoroughly. Tricky Query.
        time_limit = self.date_time

        # Get the list of previous sessions including the current session! So the list must be at least length 1 (the current session).
        # The list is sorted in descending date_time order, so that the first entry is the current sessions.
        sfilter = Q(date_time__lte=time_limit) & Q(game=self.game)
        if player:
            sfilter = sfilter & (Q(ranks__player=player) | Q(ranks__team__players=player))

        prev_sessions = Session.objects.filter(sfilter).order_by('-date_time')

        return prev_sessions

    def previous_session(self, player=None):
        '''
        Returns the previous session that the nominate player played this game in.
        Or None if no such session exists.

        :param player: A Player object. Optional, returns the last session this game was played if not provided.
        '''
        prev_sessions = self.previous_sessions(player)

        if len(prev_sessions) < 2:
            assert len(prev_sessions) == 1, f"Database error: Current session not in previous sessions list, session={self.pk}, player={player.pk}, {len(prev_sessions)=}."
            assert prev_sessions[0] == self, f"Database error: Current session not in previous sessions list, session={self.pk}, player={player.pk}, {prev_sessions=}."
            prev_session = None
        else:
            prev_session = prev_sessions[1]

            if not prev_sessions[0].id == self.id: breakpoint()

            assert prev_sessions[0].id == self.id, f"Query error: current session is not at start of previous sessions list for session={self.pk}, first previous session={prev_sessions[0].id}, player={player.pk}"
            assert prev_session.date_time < self.date_time, f"Database error: Two sessions with identical time, session={self.pk}, previous session={prev_session.pk}, player={player.pk}"

        return prev_session

    def following_sessions(self, player=None):
        '''
        Returns all the following sessions that the nominate player played (will play?) this game in.

        Always includes the current session as the first item (previous_sessions[0]).

        :param player: A Player object. Optional, all following sessions this game was played in if not provided.
        '''
        # TODO: Test thoroughly. Tricky Query.
        time_limit = self.date_time

        # Get the list of previous sessions including the current session! So the list must be at least length 1 (the current session).
        # The list is sorted in descending date_time order, so that the first entry is the current sessions.
        sfilter = Q(date_time__gte=time_limit) & Q(game=self.game)
        if player:
            sfilter = sfilter & (Q(ranks__player=player) | Q(ranks__team__players=player))

        foll_sessions = Session.objects.filter(sfilter).order_by('date_time')

        return foll_sessions

    def following_session(self, player=None):
        '''
        Returns the following session that the nominate player played this game in.
        Or None if no such session exists.

        :param player: A Player object. Optional, returns the last session this game was played if not provided.
        '''
        foll_sessions = self.following_sessions(player)

        if len(foll_sessions) < 2:
            assert len(foll_sessions) == 1, f"Database error: Current session not in following sessions list, session={self.pk}, player={player.pk}, {len(foll_sessions)=}."
            assert foll_sessions[0] == self, f"Database error: Current session not in following sessions list, session={self.pk}, player={player.pk}, {foll_sessions=}."
            foll_session = None
        else:
            foll_session = foll_sessions[1]
            assert foll_sessions[0].date_time == self.date_time, f"Query error: current session not in following sessions list of following sessions for session={self.pk}, player={player.pk}"
            assert foll_session.date_time > self.date_time, f"Database error: Two sessions with identical time, session={self.pk}, previous session={foll_session.pk}, player={player.pk}"

        return foll_session

    @property
    def is_latest(self):
        '''
        True if this is the latest session in this game for all the players who played it. That is modifying it
        would (probably) not trigger any rebuilds (clear exceptions would be if a new player was added, who does
        have a future session,  or the date_time of the session is changed to be earlier than another session in
        this game with one or more of these players, or if the game is chnaged). Basically only true if it is
        currently the latest session for all htese players in this game. Can easily change if the session is
        edited, or for that matter another one is (moved after this one for example)
        '''
        Rating = apps.get_model(APP, "Rating")

        is_latest = {}
        for performance in self.performances.all():
            rating = Rating.get(performance.player, self.game)  # Creates a new rating if needed
            is_latest[performance.player] = self.date_time == rating.last_play
            assert not self.date_time > rating.last_play, "Rating last_play seems out of sync."

        return all(is_latest.values())

    @property
    def is_first(self):
        '''
        True if this is the first session in this game (so it has no previous session).
        '''
        first = Session.objects.filter(game=self.game).order_by('date_time').first()
        is_first = self == first
        return is_first

    def previous_victories(self, player):
        '''
        Returns all the previous sessions that the nominate player played this game in that this player won
        Or None if no such session exists.

        :param player: a Player object. Required, as the previous_vitory of any player is just previous_session().
        '''
        # TODO: Test thoroughly. Tricky Query.
        time_limit = self.date_time

        # Get the list of previous sessions including the current session! So the list must be at least length 1 (the current session).
        # The list is sorted in descening date_time order, so that the first entry is the current sessions.
        sfilter = Q(date_time__lte=time_limit) & Q(game=self.game) & Q(ranks__rank=1)
        sfilter = sfilter & (Q(ranks__player=player) | Q(ranks__team__players=player))
        prev_sessions = Session.objects.filter(sfilter).order_by('-date_time')

        return prev_sessions

    def rank(self, player):
        '''
        Returns the Rank object for the nominated player in this session
        '''
        if self.team_play:
            ranks = self.ranks.filter(team__players=player)
        else:
            ranks = self.ranks.filter(player=player)

        # 2 or more ranks for this player is a database integrity failure. Something serious got broken.
        assert len(ranks) < 2, "Database error: {} Ranks objects in database for session={}, player={}".format(len(ranks), self.pk, player.pk)

        # Could be called before rank objects for a session submission were saved, In which case nicely indicate so with None.
        return ranks[0] if len(ranks) == 1 else None

    def performance(self, player):
        '''
        Returns the Performance object for the nominated player in this session
        '''
        assert player != None, f"Coding error: Cannot fetch the performance of 'no player'. Session pk: {self.pk}"
        performances = self.performances.filter(player=player)
        assert len(performances) == 1, "Database error: {} Performance objects in database for session={}, player={} sql={}".format(len(performances), self.pk, player.pk, performances.query)
        return performances[0]

    def previous_performance(self, player):
        '''
        Returns the previous Performance object for the nominate player in the game of this session
        '''
        prev_session = self.previous_session(player)
        return None if prev_session is None else prev_session.performance(player)

    def previous_victory(self, player):
        '''
        Returns the last Performance object for the nominate player in the game of this session that was victory
        '''
        # TODO: Test thoroughly. Tricky Query.
        time_limit = self.date_time

        # Get the list of previous sessions including the current session! So the list must be at least length 1 (the current session).
        # The list is sorted in descening date_time order, so that the first entry is the current sessions.
        prev_victory = Session.objects.filter(Q(date_time__lte=time_limit) & Q(game=self.game) & Q(ranks__rank=1) & (Q(ranks__player=player) | Q(ranks__team__players=player))).order_by('-date_time')
        return None if (prev_victory is None or prev_victory.count() == 0) else prev_victory[0].performance(player)

    def clean_ranks(self):
        '''
        Ranks can be submitted any which way, all that matters is that they can order the players
        and identify ties. For consistency though in the database we can enforce clean rankings.

        Two strategies are possible, strictly sequential,or sequential with tie gaps. To illustrate
        with a 6 player game and a tie for 2nd place:

        sequential:  1, 2, 2, 3, 4, 5
        tie gapped:  1, 2, 2, 4, 5, 6

        This cleaner will create tie gapped ranks.
        '''
        if settings.DEBUG:
            # Grab a pre snapshot
            rank_debug_pre = {}
            for rank in self.ranks.all():
                rkey = rank.team.pk if self.team_play else rank.player.pk
                rank_debug_pre[f"{'Team' if self.team_play else f'Player'} {rkey}"] = rank.rank

            log.debug(f"\tRanks Before: {sorted(rank_debug_pre.items(), key=lambda x: x[1])}")

        # First collect all the supplied ranks
        rank_values = []
        ranks_by_pk = {}
        for rank in self.ranks.all():
            rank_values.append(rank.rank)
            ranks_by_pk[rank.pk] = rank.rank
        # Then sort them by rank
        rank_values.sort()

        if settings.DEBUG:
            log.debug(f"\tRank values: {rank_values}")
            log.debug(f"\tRanks by PK: {ranks_by_pk}")

        # Build a map of submited ranks to saving ranks
        rank_map = OrderedDict()

        if settings.DEBUG:
            log.debug(f"\tBuilding rank map")
        expected = 1
        for rank in rank_values:
            # if it's a new rank process it
            if not rank in rank_map:
                # If we have the expected value map it to itself
                if rank == expected:
                    rank_map[rank] = rank
                    expected += 1
                    if settings.DEBUG:
                        log.debug(f"\t\tRank {rank} is as expected.")

                # Else map all tied ranks to the expected value and update the expectation
                else:
                    if settings.DEBUG:
                        log.debug(f"\t\tRank {rank} is expected at {expected}.")
                    rank_map[rank] = expected
                    expected += rank_values.count(rank)
                    if settings.DEBUG:
                        log.debug(f"\t\t\tMoved {rank_values.count(rank)} {'teams' if self.team_play else f'players'} to the expected rank and the new expectation is {expected}.")

        if settings.DEBUG:
            log.debug(f"\tRanks Map: {rank_map}")

        for From, To in rank_map.items():
            if not From == To:
                pks = [k for k, v in ranks_by_pk.items() if v == From]
                rank_objs = self.ranks.filter(pk__in=pks)
                for rank_obj in rank_objs:
                    rank_obj.rank = To
                    rank_obj.save()
                    rkey = rank_obj.team.pk if self.team_play else rank_obj.player.pk
                    if settings.DEBUG:
                        log.debug(f"\tMoved {'Team' if self.team_play else f'Player'} {rkey} from rank {rank} to {rank_obj.rank}.")

        if settings.DEBUG:
            # Grab a pre snapshot
            rank_debug_post = {}
            for rank_obj in self.ranks.all():
                rkey = rank_obj.team.pk if self.team_play else rank_obj.player.pk
                rank_debug_post[f"{'Team' if self.team_play else f'Player'} {rkey}"] = rank_obj.rank

            log.debug(f"\tRanks Before : {sorted(rank_debug_pre.items(), key=lambda x: x[1])}")
            log.debug(f"\tRanks Cleaned: {sorted(rank_debug_post.items(), key=lambda x: x[1])}")

    def build_trueskill_data(self, save=False):
        '''Builds a the data structures needed by trueskill.rate

        if save is True, will initialise Performance objects for each player too.

         A RatingGroup is list of dictionaries, one dictionary for each team
            keyed on the team name or ID containing a trueskill Rating object for that team
         In single player mode we simply supply teams of 1, so each dictionary has only one member
             and can be keyed on player name or ID.
         A trueskill Rating is just a trueskill mu and sigma pair (actually a Gaussian object with a mu and sigma).

        Weights is a dictionary, keyed on a player identifier with a weight as a value
            The weights are 0 to 1, 0 meaning no play and 1 meaning full time play.
            The player identifier is is a tuple which has two values (RatingsGroup index, Key into that RatingsGroup dictionary)

        Ranking list is a list of ranks (1, 2, 3 for first, second, third etc) that maps item
            for item into RatingGroup. Ties are indicated by repeating a given rank.
         '''
        RGs = []
        Weights = {}
        Ranking = []

        if self.team_play:
            for rank, team in self.ranked_teams.items():
                RG = {}
                RGs.append(RG)
                for player in team.players.all():
                    performance = self.performance(player)
                    if self.__bypass_admin__:
                        performance.__bypass_admin__ = True
                    performance.initialise(save)
                    RG[player.pk] = trueskill.Rating(mu=performance.trueskill_mu_before, sigma=performance.trueskill_sigma_before)
                    Weights[(len(RGs) - 1, player.pk)] = performance.partial_play_weighting
                Ranking.append(int(rank.split('.')[0]))
        else:
            for rank, player in self.ranked_players.items():
                performance = self.performance(player)
                if self.__bypass_admin__:
                    performance.__bypass_admin__ = True
                performance.initialise(save)
                RGs.append({player.pk: trueskill.Rating(mu=performance.trueskill_mu_before, sigma=performance.trueskill_sigma_before)})
                Weights[(len(RGs) - 1, player.pk)] = performance.partial_play_weighting
                Ranking.append(int(rank.split('.')[0]))
        return RGs, Weights, Ranking

    def calculate_trueskill_impacts(self):
        '''
        Given the rankings associated with this session (i.e. assuming they are recorded)
        and the trueskill measures for each player before the session will, calculate (and
        record against this session) on their basis the new trueskill measures.

        Saves the impacts to the database in the form of Performance objects and returns a
        summary of impacts.

        Does not update ratings in the database.
        '''
        Player = apps.get_model(APP, "Player")
        Performance = apps.get_model(APP, "Performance")

        TSS = TrueskillSettings()
        TS = trueskill.TrueSkill(mu=TSS.mu0, sigma=TSS.sigma0, beta=self.game.trueskill_beta, tau=self.game.trueskill_tau, draw_probability=self.game.trueskill_p)

        def RecordPerformance(rating_groups):
            '''
            Given a rating_groups structure from trueskill.rate will distribute the results to the Performance objects

            The Trueskill impacts are extracted from the rating_groups recorded in Performance objects.

            Ratings are not updated here. These are used to update ratings elsewhere.
            '''
            for t in rating_groups:
                for p in t:
                    player = Player.objects.get(pk=p)

                    performances = Performance.objects.filter(session=self, player=player)
                    assert len(performances) == 1, "Database error: {} Performance objects in database for session={}, player={}".format(len(performances), self.pk, player.pk)
                    performance = performances[0]

                    mu = t[p].mu
                    sigma = t[p].sigma

                    performance.trueskill_mu_after = mu
                    performance.trueskill_sigma_after = sigma
                    performance.trueskill_eta_after = mu - TSS.mu0 / TSS.sigma0 * sigma  # µ − (µ0 ÷ σ0) × σ

                    # eta_before was saved when the performance ws initialised from the previous performance.
                    # We recalculate it now as an integrity check against global TrueSkill settings change.
                    # A change in eta_before suggests one of the global values TSS.mu0 or TSS.sigma0 has changed
                    # and that is a conditon that needs handling. In theory it should force a complete rebuild
                    # of the ratings. For now, just throw an exception.
                    # TODO: Handle changes in TSS.mu0 or TSS.sigma0 cleanly. Namely:
                    #    trigger a neat warning to the registrar (person saving a session now)
                    #    inform admins by email, with suggested action (rebuild ratings from scratch or reset TSS.mu0 and TSS.sigma0
                    previous_trueskill_eta_before = performance.trueskill_eta_before
                    performance.trueskill_eta_before = performance.trueskill_mu_before - TSS.mu0 / TSS.sigma0 * performance.trueskill_sigma_before
                    assert isclose(performance.trueskill_eta_before, previous_trueskill_eta_before, abs_tol=FLOAT_TOLERANCE), "Integrity error: suspiscious change in a TrueSkill rating."

                    if self.__bypass_admin__:
                        performance.__bypass_admin__ = True

                    performance.save()
            return

        # Trueskill Library has moderate internal docs. Much better docs here:
        #    http://trueskill.org/
        # For our sanity to be clear here:
        #
        # RatingsGroup is a list each item of which is a dictionary,
        #    keyed on player ID with a rating object as its value
        #    teams are supported by this list, that is each item in RatingsGroup
        #    is a logical player or team represented by a dicitonary of players.
        #    With individual players the team simply has one item in the dictionary.
        #    Teams with more than one player have all the players in this dictionary.
        #
        # Ranking is a list of rankings. Each list item maps into a RatingsGroup list item
        #    so the 0th value in Rank maps to the 0th value in RatingsGroup
        #    and the 1st value in Rank maps to the 0th value in RatingsGroup etc.
        #    Each item in this list is a numeric (int) ranking.
        #    Ties are recorded with the same ranking value. Equal 1 for example.
        #    The value of the rankings is relevant only for sorting, that is ordering the
        #    objects in the RatingsGroup list (and supporting ties).
        #
        # Weights is a dictionary, keyed on a player identifier with a weight as a value
        #    The weights are 0 to 1, 0 meaning no play and 1 meaning full time play.
        #    The player identifier is is a tuple which has two values (RatingsGroup index, Key into that RatingsGroup dictionary)

        OldRatingGroups, Weights, Ranking = self.build_trueskill_data(save=True)
        NewRatingGroups = TS.rate(OldRatingGroups, Ranking, Weights, TSS.delta)
        RecordPerformance(NewRatingGroups)

        return self.trueskill_impacts

    def __unicode__(self):
        return f'{time_str(self.date_time)} - {self.game}'

    def __str__(self): return self.__unicode__()

    def __verbose_str__(self):
        return u'{} - {} - {} - {}'.format(
            time_str(self.date_time),
            self.league,
            self.location,
            self.game)

    def __rich_str__(self, link=None):
        url_view_self = reverse('view', kwargs={'model': self._meta.model_name, 'pk': self.pk}) if link == flt.internal else None

        if self.team_play:
            victors = []
            for t in self.victors:
                if t.name is None:
                    victors += ["(" + ", ".join([field_render(p.name_nickname, link_target_url(p, link)) for p in t.players.all()]) + ")"]
                else:
                    victors += [field_render(t.name, link_target_url(t, link))]
        else:
            victors = [field_render(p.name_nickname, link_target_url(p, link)) for p in self.victors]

        try:
            V = ", ".join(victors)
            # venue = f"- {field_render(self.location, link)}"
            T = time_str(self.date_time)
            if url_view_self:
                T = f"<a href='{url_view_self}' class='field_link'>{T}</a>"

            return (f'{T} - {field_render(self.game, link)} - {self.num_competitors} {self.str_competitors} ({self.str_ranked_players()}) - {V} won')
        except:
            pass

    def __detail_str__(self, link=None):
        url_view_self = reverse('view', kwargs={'model': self._meta.model_name, 'pk': self.pk}) if link == flt.internal else None

        T = time_str(self.date_time)
        if url_view_self:
            T = f"<a href='{url_view_self}' class='field_link'>{T}</a>"

        detail = T + "<br>"
        detail += field_render(self.game, link) + "<br>"
        detail += u'<OL>'

        rankers = OrderedDict()
        for r in self.ranks.all():
            if self.team_play:
                ranker = field_render(r.team, link)
            else:
                ranker = field_render(r.player, link)

            if r.rank in rankers:
                rankers[r.rank].append(ranker)
            else:
                rankers[r.rank] = [ranker]

        for rank in rankers:
            detail += u'<LI value={}>{}</LI>'.format(rank, ", ".join(rankers[rank]))

        detail += u'</OL>'
        return detail

    @property
    def dict_from_object(self):
        '''
        Returns a dictionary that represents this object (so that it can be serialized).

        Django has an internal function
            django.forms.models.model_to_dict()
        that does similar but is far more generic retuning a dict of model fields only,
        in the case of this model: game, date_time, league, location and team_play.

        In support of rich objects we need to customise this dict really to include
        related information as we have here. This dict defines a Session instance for
        example where model_to_dict() fails to.
        '''
        # Convert the session to serializable form (a dict)
        ranks = [r.pk for r in self.ranks.all().order_by("rank")]
        rankings = [r.rank for r in self.ranks.all().order_by("rank")]

        # rankers is a list of team or player Pks based on the mode
        if self.team_play:
            rankers = [r.team.pk for r in self.ranks.all().order_by("rank")]
        else:
            rankers = [r.player.pk for r in self.ranks.all().order_by("rank")]

        performances = [p.pk for p in self.performances.all().order_by("player__pk")]
        performers = [p.player.pk for p in self.performances.all().order_by("player__pk")]
        weights = [p.partial_play_weighting for p in self.performances.all().order_by("player__pk")]

        # Createa serializeable form of the (rich) object
        return { "model": self._meta.model.__name__,
                 "id": self.pk,
                 "game": self.game.pk,
                 "time": self.date_time_local,
                 "league": self.league.pk,
                 "location": self.location.pk,
                 "team_play": self.team_play,
                 "ranks": ranks,
                 "rankings": rankings,
                 "rankers": rankers,
                 "performances": performances,
                 "performers": performers,
                 "weights": weights}

    @classmethod
    def dict_from_form(cls, form_data, pk=None):
        '''
        Returns a dictionary that represents this form data supplied.

        This centralises form parsing for this model and provides a
        dict that compares with dict_from_object() above to facilitate
        change detection on form submissions.

        :param form_data: A Django QueryDict representing a form submission
        :param pk: Optionally a Prinary Key to add to the dict
        '''
        # Extract the form data we need
        game = int(form_data.get("game", MISSING_VALUE))
        time = make_aware(parser.parse(form_data.get("date_time", NEVER)))
        league = int(form_data.get("league", MISSING_VALUE))
        location = int(form_data.get("location", MISSING_VALUE))
        team_play = 'team_play' in form_data

        # We expect the ranks and performances to arrive in Django formsets
        # The forms in the formsets are not in any guranteed order so we collect
        # data first and sort it.
        num_ranks = int(form_data.get('Rank-TOTAL_FORMS', 0))
        rank_data = sorted([(int(form_data.get(f'Rank-{r}-id', MISSING_VALUE)),
                             int(form_data.get(f'Rank-{r}-rank', MISSING_VALUE)),
                             int(form_data.get(f'Rank-{r}-team' if team_play else f'Rank-{r}-player', MISSING_VALUE))
                             ) for r in range(num_ranks)], key=lambda e: e[1])  # Sorted by rank

        num_performances = int(form_data.get('Performance-TOTAL_FORMS', 0))
        performance_data = sorted([(int(form_data.get(f'Performance-{p}-id', MISSING_VALUE)),
                             int(form_data.get(f'Performance-{p}-player', MISSING_VALUE)),
                             float(form_data.get(f'Performance-{p}-partial_play_weighting', MISSING_VALUE))
                             ) for p in range(num_performances)], key=lambda e: e[1])  # Sorted by player ID

        return { "model": cls.__name__,
                 "id": pk if pk else MISSING_VALUE,
                 "game": game,
                 "time": time,
                 "league": league,
                 "location": location,
                 "team_play": team_play,
                 "ranks": [r[0] for r in rank_data],
                 "rankings": [r[1] for r in rank_data],
                 "rankers": [r[2] for r in rank_data],
                 "performances": [p[0] for p in performance_data],
                 "performers": [p[1] for p in performance_data],
                 "weights": [p[2] for p in performance_data]}

    @property_method
    def dict_delta(self, form_data=None):
        '''
        Given form data (a QueryDict) will return a dict that merges
        dict_from_object and dict_from_form respectively into one delta
        dict that captures a summary of changes.

        If no form_data is supplied just returns dict_from_object.

        :param form_data: A Django QueryDict representing a form submission
        '''
        from_object = self.dict_from_object
        result = from_object.copy()

        if form_data:
            # dict_from_form is a class method so it is available when no model instance is.
            from_form = self._meta.model.dict_from_form(form_data)

            # Find what changed and take note of it (replaceing the data bvalue with two-tuple and adding the key to the changed set)
            changes = set()

            def check(key):
                if from_form[key] != from_object[key]:
                    # If the form fails to specifiy an Id we assume it refers to
                    # this instance and don't note the absence as a change
                    if not (key == "id" and from_form[key] == MISSING_VALUE):
                        changes.add(key)
                    result[key] = (from_object[key], from_form[key])

            for key in result:
                check(key)

            # Some changes are expected to have no impact on leaderboards (for example different location
            # and league - as boards are global and leagues only used for filtering views). Other changes
            # impact the leaderboard. If any of those chnage we add a psudo_field "leaderboard" in changes.
            cause_leaderboard_change = ["game", "team_play", "rankers", "performers", "weights"]
            for change in changes:
                if change in cause_leaderboard_change:
                    changes.add("leaderboard")
                    break

            # The date_time is a little trickier as it can change as long as the immediately preceding session stays
            # the same. If that changes, because this date_time change brings it before the existing one puts anotehr
            # session there (by pushing this session past another) then it will change the leaderboard_before and
            # hence the leaderboard_after. We can't know this from the delta but we can divine it from this session.

            result["changes"] = tuple(changes)

        return result

    def __json__(self, form_data=None, delta=None):
        '''
        A basic JSON serializer

        If form data is supplied willl build a chnage description by replacing each changed
        value with a 2-tuple containing the object value and recording which values changed
        (and are now 2-tuples) in the "changes" element.

        :param form_data: optionally submitted form data. A Django QueryDict.
        :param delta: a self.dict_delta if it's already been produced which is used in place of form_data and simply JSONified.
        '''
        if not delta:
            delta = self.dict_delta(form_data)

        return json.dumps(delta, cls=DjangoJSONEncoder)

    def check_integrity(self, passthru=True):
        '''
        It should be impossible for a session to go wrong if implemented securely and atomically.

        But all the same it's a complex object and database integrity failures can cause a lot of headaches,
        so this is a centralised integrity check for a given session so that a walk through sessions can find
        and identify issues easily.
        '''
        L = AssertLog(passthru)

        pfx = f"Session Integrity error (id: {self.id}):"

        # Check all the fields
        for field in ['date_time', 'league', 'location', 'game', 'team_play']:
            L.Assert(getattr(self, field, None) != None, f"{pfx} Must have {field}.")

        # Check that the play mode is supported by the game
        L.Assert(not self.team_play or self.game.team_play, f"{pfx} Recorded with team play, but Game (ID: {self.game.id}) does not support that!")
        L.Assert(self.team_play or self.game.individual_play, f"{pfx} Recorded with individual play, but Game (ID: {self.game.id}) does not support that!")

        # Check that the date_time is in the past! It makes no sense to have future sessions recorded!
        L.Assert(self.date_time <= datetime.now(tz=self.date_time_tz), f"{pfx} Session is in future! Recorded sessions must be in the past!")

        # Collect the ranks and check rank fields
        rank_values = []
        L.Assert(self.ranks.count() > 0, f"{pfx}  Has no ranks.")

        for rank in self.ranks.all():
            for field in ['session', 'rank']:
                L.Assert(getattr(rank, field, None) != None, f"{pfx}  Rank {rank.rank} (id: {rank.id}) must have {field}.")

            if self.team_play:
                L.Assert(getattr(rank, 'team', None) != None, f"{pfx}  Rank {rank.rank} (id: {rank.id}) must have team.")
            else:
                L.Assert(getattr(rank, 'player', None) != None, f"{pfx}  Rank {rank.rank} (id: {rank.id}) must have player.")

            rank_values.append(rank.rank)

        # Check that we have a victor
        L.Assert(1 in rank_values, f"{pfx}  Must have a victor (rank=1).")

        # Check that ranks are contiguous
        last_rank_val = 0
        rank_values.sort()
        rank_list = ', '.join([str(r) for r in rank_values])
        skip = 0
        # Supports both odered ranking and tie-gap ordered ranking
        # As an example of a six player game:
        # ordered:         1, 2, 2, 3, 4, 5
        # tie-gap ordered: 1, 2, 2, 4, 5, 6
        for rank in rank_values:
            L.Assert(rank == last_rank_val or rank == last_rank_val + 1 or rank == last_rank_val + 1 + skip, f"{pfx} Ranks must be consecutive. Found rank {rank} following rank {last_rank_val} in ranks {rank_list}. Expected it at {last_rank_val}, {last_rank_val+1} or {last_rank_val+1+skip}.")

            if rank == last_rank_val:
                skip += 1
            else:
                skip = 0
                last_rank_val = rank

        # Collect all the players (respecting the mode of play team/individual)
        players = set()
        if self.team_play:
            for rank in self.ranks.all():
                L.Assert(getattr(rank, 'team', None) != None, f"{pfx} Rank {rank.rank} (id:{rank.id}) has no team.")
                L.Assert(getattr(rank.team, 'players', 0), f"{pfx} Rank {rank.rank} (id:{rank.id}) has a team (id:{rank.team.id}) with no players.")

                # Check that the number of players is allowed by the game
                num_players = len(rank.team.players.all())
                L.Assert(num_players >= self.game.min_players_per_team, f"{pfx} Too few players in team (game: {self.game.id}, team: {rank.team.id}, players: {num_players}, min: {self.game.min_players_per_team}).")
                L.Assert(num_players <= self.game.max_players_per_team, f"{pfx} Too many players in team (game: {self.game.id}, team: {rank.team.id}, players: {num_players}, max: {self.game.max_players_per_team}).")

                for player in rank.team.players.all():
                    L.Assert(player, f"{pfx} Rank {rank.rank} (id: {rank.id}) has a team (id: {rank.team.id}) with an invalid player.")
                    players.add(player)
        else:
            for rank in self.ranks.all():
                L.Assert(rank.player, f"{pfx} Rank {rank.rank} (id: {rank.id}) has no player.")
                players.add(rank.player)

        # Check that the number of players is allowed by the game
        L.Assert(len(players) >= self.game.min_players, f"{pfx} Too few players (game: {self.game.id}, players: {len(players)}).")
        L.Assert(len(players) <= self.game.max_players, f"{pfx} Too many players (game: {self.game.id}, players: {len(players)}).")

        # Check that there's a performance obejct for each player and only one for each player
        for performance in self.performances.all():
            for field in ['session', 'player', 'partial_play_weighting', 'play_number', 'victory_count']:
                L.Assert(getattr(performance, field, None) != None, f"{pfx} Performance {performance.play_number} (id:{performance.id}) has no {field}.")

            L.Assert(performance.player in players, f"{pfx} Performance {performance.play_number} (id:{performance.id}) refers to a player that was not ranked: {performance.player}.")
            players.discard(performance.player)

        L.Assert(len(players) == 0, f"{pfx} Ranked players that lack a performance: {players}.")

        # Check that for each performance object the _before ratings are same as _after retings in the previous performance
        for performance in self.performances.all():
            previous = self.previous_performance(performance.player)
            if previous is None:
                TS = TrueskillSettings()

                trueskill_eta = TS.mu0 - TS.mu0 / TS.sigma0 * TS.sigma0

                L.Assert(isclose(performance.trueskill_mu_before, TS.mu0, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance µ mismatch. Before at {performance.session.date_time} is {performance.trueskill_mu_before} and After on previous at Never is {TS.mu0} (the default)")
                L.Assert(isclose(performance.trueskill_sigma_before, TS.sigma0, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance σ mismatch. Before at {performance.session.date_time} is {performance.trueskill_sigma_before} and After on previous at Never is {TS.sigma0} (the default)")
                L.Assert(isclose(performance.trueskill_eta_before, trueskill_eta, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance η mismatch. Before at {performance.session.date_time} is {performance.trueskill_eta_before} and After on previous at Never is {trueskill_eta} (the default)")
            else:
                L.Assert(isclose(performance.trueskill_mu_before, previous.trueskill_mu_after, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance µ mismatch. Before at {performance.session.date_time} is {performance.trueskill_mu_before} and After on previous at {previous.session.date_time} is {previous.trueskill_mu_after}")
                L.Assert(isclose(performance.trueskill_sigma_before, previous.trueskill_sigma_after, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance σ mismatch. Before at {performance.session.date_time} is {performance.trueskill_sigma_before} and After on previous at {previous.session.date_time} is {previous.trueskill_sigma_after}")
                L.Assert(isclose(performance.trueskill_eta_before, previous.trueskill_eta_after, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance η mismatch. Before at {performance.session.date_time} is {performance.trueskill_eta_before} and After on previous at {previous.session.date_time} is {previous.trueskill_eta_after}")

        return L.assertion_failures

    def clean(self):
        '''
        Clean is called by Django before form_valid is called for the form. It affords a place and way for us to
        Check that everything is in order before proceding to the form_valid method that should save.
        '''
        # Check that the number of players is allowed by the game
        # This is called before the ranks are saved and hence fails always!
        # The bounce also loses the player selections (and maybe more form the Performance widgets?
        # FIXME: While bouncing, see what we can do to conserve the form, state!
        # FIXME: Fix the bounce, namely work out how to test the related objects in the right order of events

        # FIXME: When we land here no ranks or performances are saved, and
        # self.players finds no related ranks.
        # Does this mean we need to do an is_valid and if so, save on the
        # ranks and performances first? But if the session is not saved they
        # too will have dramas with order.
        #
        # Maybe it's hard with clean (which is presave) to do the necessary
        # relation tests? Is this an application for an atomic save, which
        # can be performed on all the forms with minimal clean, then
        # subsequently an integrity check (or clean) on the enseble and
        # if failed, then roll back?

        # For now bypass the clean to do a test
        return

        players = self.players
        nplayers = len(players)
        if nplayers < self.game.min_players:
            raise ValidationError("Session {} has fewer players ({}) than game {} demands ({}).".format(self.pk, nplayers, self.game.pk, self.game.min_players))
        if nplayers > self.game.max_players:
            raise ValidationError("Session {} has more players ({}) than game {} permits ({}).".format(self.pk, nplayers, self.game.pk, self.game.max_players))

        # Ensure the play mode is compatible with the game being played. Form should have enforced this,
        # but we ensure it here.
        if (self.team_play and not self.game.team_play):
            raise ValidationError("Session {} specifies team play when game {} does not support it.".format(self.pk, self.game.pk))

        if (not self.team_play and not self.game.individual_play):
            raise ValidationError("Session {} specifies individual play when game {} does not support it.".format(self.pk, self.game.pk))

        # Ensure the time of the session does not clash. We need for this game and for any of these players for
        # session time to be unique so that when TruesKill ratings are calculated the session times for all
        # affected players have a clear order. Unrelated sessions that don't involve the same game or any of
        # this sessions players can have an identical time and this won't affect the ratings.

        # Now force a unique time for this game and these players
        # We just keep adding a millisecond to the time while there are coincident sessions
        while True:
            dfilter = Q(date_time=self.date_time)
            gfilter = Q(game=self.game)

            pfilter = Q()
            for player in players:
                pfilter |= Q(performances__player=player)

            sfilter = dfilter & gfilter & pfilter

            coincident_sessions = Session.objects.filter(sfilter).exclude(pk=self.pk)

            if coincident_sessions.count() > 0:
                self.date_time += MIN_TIME_DELTA
            else:
                break

        # Collect the ranks and check rank fields
        rank_values = []

        if self.ranks.count() == 0:
            raise ValidationError("Session {} has no ranks.".format(self.id))

        for rank in self.ranks.all():
            rank_values.append(rank.rank)

        # Check that we have a victor
        if not 1 in rank_values:
            raise ValidationError("Session {} has no victor (rank = 1).".format(self.id))

        # Check that ranks are contiguous
        last_rank_val = 0
        rank_values.sort()
        for rank in rank_values:
            if not (rank == last_rank_val or rank == last_rank_val + 1):
                raise ValidationError("Session {} has a gap in ranks (between {} and {})".format(self.id), last_rank_val, rank)
            last_rank_val = rank

    def clean_relations(self):
        pass
#         errors = {
#             "date_time": ["Bad DateTime"],
#             "league": ["Bad League", "No not really"],
#             NON_FIELD_ERRORS: ["One error", "Two errors"]
#             }
#         raise ValidationError(errors)

    class Meta(AdminModel.Meta):
        verbose_name = "Session"
        verbose_name_plural = "Sessions"
        ordering = ['-date_time']

