# Python packages
import trueskill, html, re, pytz, os, json

from collections import OrderedDict
from sortedcontainers import SortedDict
from math import isclose
from scipy.stats import norm
from statistics import mean, stdev
from datetime import datetime, timedelta
from dateutil import parser
from builtins import str
from enum import Enum

# Local packages
from .trueskill_helpers import TrueSkillHelpers  # Helper functions for TrueSkill, based on "Understanding TrueSkill"
from .leaderboards import styled_player_list, restyle_leaderboard, augment_with_deltas, player_ratings, player_rankings, LB_STRUCTURE, LB_PLAYER_LIST_STYLE, immutable, mutable

# Django packages
from django.db import models, DataError, IntegrityError  # , connection,
from django.db.models import Sum, Max, Avg, Count, Q, F, OuterRef, Subquery, ExpressionWrapper
from django.db.models.expressions import Func
from django.conf import settings
from django.core.exceptions import ValidationError, ObjectDoesNotExist, MultipleObjectsReturned  # , PermissionDenied, NON_FIELD_ERRORS
from django.core.validators import RegexValidator
from django.core.serializers.json import DjangoJSONEncoder
from django.urls import reverse  # , reverse_lazy
from django.db.models.query import QuerySet
from django.contrib import admin
from django.contrib.auth.models import User
from django.utils import timezone
from django.utils.formats import localize
from django.utils.timezone import localtime
from django.utils.safestring import mark_safe

from bitfield import BitField
from bitfield.forms import BitFieldCheckboxSelectMultiple
from timezone_field import TimeZoneField
from mapbox_location_field.models import LocationField
from relativefilepathfield.fields import RelativeFilePathField

from django_model_admin_fields import AdminModel
from django_model_privacy_mixin import PrivacyMixIn

from django_generic_view_extensions import FIELD_LINK_CLASS
from django_generic_view_extensions.options import flt, osf
from django_generic_view_extensions.model import field_render, link_target_url, safe_get, TimeZoneMixIn
from django_generic_view_extensions.decorators import property_method
from django_generic_view_extensions.datetime import time_str, make_aware, safe_tz
from django_generic_view_extensions.queryset import get_SQL
from django_generic_view_extensions.util import AssertLog, pythonify
from django_generic_view_extensions.html import NEVER

from CoGs.logging import log

# TODO: Next round of model enhancements
#
# Add expected play time to Game
# Add a location (lat/lon) field to Location.

# TODO: Use @cached_property in place of @property everywhere. See no reason not to!

# CoGs Leaderboard Server Data Model
#
# The underlying model of data is designed designed to allow:
#
# Game sessions to be recorded by a registrar
# TrueSkill ratings for a player on a game to be calculable from the session records.
# So that leaderboards can be presented for any game in any league (a distinct set of players who are competing)
# Consistent TrueSkill ratings across leagues so that a global leaderboard can be generated as well
# BoardGameGeek connections support for games and players
#
# Note this file defines the data model in Python syntax and a migration (Sync DB)
# converts it into a database schema (table definitions).

MAX_NAME_LENGTH = 200  # The maximum length of a name in the database, i.e. the char fields for player, game, team names and so on.
FLOAT_TOLERANCE = 0.0000000000001  # Tolerance used for comparing float values of Trueskill settings and results between two objects when checking integrity.

# Some reserved names for ALL objects in a model (note ID=0 is reserved for the same meaning).
ALL_LEAGUES = "Global"  # A reserved key in leaderboard dictionaries used to represent "all leagues" in some requests
ALL_PLAYERS = "Everyone"  # A reserved key for leaderboard filtering representing all players
ALL_GAMES = "All Games"  # A reserved key for leaderboard filtering representing all games

# TODO: consider a special league that filters all "My" thins
# So on list views all things I am involved in (my leagues, my games, my players, my locations, my sessions, etc) and on Leaderboards too.
MY_PSEUDO_LEAGUE = "Mine"

MIN_TIME_DELTA = timedelta.resolution  # A nominally smallest time delta we'll consider.

MISSING_VALUE = -1  # Used primarily for PKs (which are  never negative)


#===============================================================================
# A structured approach to rating rebuild logging
#
# An approach to defining/recording what triggers a rating rebuild
#===============================================================================
class RATING_REBUILD_TRIGGER(Enum):
    user_request = 0  # A rating rebuild was explicitly requested by an authorized user.
    session_add = 1  # A rating rebuild was triggered by a newly added session
    session_edit = 2  # A rating rebuild was triggered by a session edit
    session_delete = 3  # A rating rebuild was triggered by a session deletion

    choices = (
        (user_request, 'User Request'),
        (session_add, 'Session Add'),
        (session_edit, 'Session Edit'),
        (session_delete, 'Session Delete')
    )

    labels = {c[0]:c[1] for c in choices}

#===============================================================================
# The support models, that store all the play records that are needed to
# calculate and maintain TruesKill ratings for players.
#===============================================================================


class TrueskillSettings(models.Model):
    '''
    The site wide TrueSkill settings to use (i.e. not Game).
    '''
    # Changing these affects the entire ratings history. That is we change one of these settings, either:
    #    a) All the ratings history needs to be recalculated to get a consistent ratings result based on the new settings
    #    b) We keep the ratings history with the old values and move forward with the new
    # Merits? Methods?
    # Suggest we have a form for editing these with processing logic and don't let the admin site edit them, or create logic
    # that can adapt to admin site edits - flagging the likelihood. Or perhaps we should make this not a one tuple table
    # but store each version with a date. That in can of course then support staged ratings histories, so not a bad idea.
    mu0 = models.FloatField('TrueSkill Initial Mean (µ0)', default=trueskill.MU)
    sigma0 = models.FloatField('TrueSkill Initial Standard Deviation (σ0)', default=trueskill.SIGMA)
    delta = models.FloatField('TrueSkill Delta (δ)', default=trueskill.DELTA)

    add_related = None

    def __unicode__(self): return u'µ0={} σ0={} δ={}'.format(self.mu0, self.sigma0, self.delta)

    def __str__(self): return self.__unicode__()

    class Meta:
        verbose_name = "Trueskill settings"
        verbose_name_plural = "Trueskill settings"

#===============================================================================
# The Ratings model(s) where TrueSkill ratings are stored
#===============================================================================


class RatingModel(TimeZoneMixIn, AdminModel):
    '''
    A Trueskill rating for a given Player at a give Game.

    This is the ultimate goal of the whole exercise. To record game sessions in order to calculate
    ratings for players and rank them in leaderboards.

    Every player has a rating at every game, though only those deviating from default (i.e. games
    that a player has players) are stored in the database.

    This is an abstract model defining the table structure that us used
    by Rating and BackupRating. The latter being a place to copy Rating
    before a complete rebuild of ratings.

    The preferred way of fetching a Rating is through Player.rating(game) or Game.rating(player).
    '''
    player = models.ForeignKey('Player', verbose_name='Player', related_name='%(class)ss', on_delete=models.CASCADE)  # If the player is deleted delete the raiting
    game = models.ForeignKey('Game', verbose_name='Game', related_name='%(class)ss', on_delete=models.CASCADE)  # If the game is deleted delete the raiting

    plays = models.PositiveIntegerField('Play Count', default=0)
    victories = models.PositiveIntegerField('Victory Count', default=0)

    last_play = models.DateTimeField('Time of Last Play', default=NEVER)
    last_play_tz = TimeZoneField('Time of Last Play, Timezone', default=settings.TIME_ZONE, editable=False)
    last_victory = models.DateTimeField('Time of Last Victory', default=NEVER)
    last_victory_tz = TimeZoneField('Time of Last Victory, Timezone', default=settings.TIME_ZONE, editable=False)

    # Although Eta (η) is a simple function of Mu (µ) and Sigma (σ), we store it alongside Mu and Sigma because it is also a function of global settings µ0 and σ0.
    # To protect ourselves against changes to those global settings, or simply to detect them if it should happen, we capture their value at time of rating update in the Eta.
    # These values before each game session and their new values after a game session are stored with the Session Ranks for integrity and history plotting.
    trueskill_mu = models.FloatField('Trueskill Mean (µ)', default=trueskill.MU, editable=False)
    trueskill_sigma = models.FloatField('Trueskill Standard Deviation (σ)', default=trueskill.SIGMA, editable=False)
    trueskill_eta = models.FloatField('Trueskill Rating (η)', default=trueskill.SIGMA, editable=False)

    # Record the global TrueskillSettings mu0, sigma0 and delta with each rating as an integrity measure.
    # They can be compared against the global settings and and difference can trigger an update request.
    # That is, flag a warning and if they are consistent across all stored ratings suggest TrueskillSettings
    # should be restored (and offer to do so?) or if inconsistent (which is an integrity error) suggest that
    # ratings be globally recalculated
    trueskill_mu0 = models.FloatField('Trueskill Initial Mean (µ)', default=trueskill.MU, editable=False)
    trueskill_sigma0 = models.FloatField('Trueskill Initial Standard Deviation (σ)', default=trueskill.SIGMA, editable=False)
    trueskill_delta = models.FloatField('TrueSkill Delta (δ)', default=trueskill.DELTA)

    # Record the game specific Trueskill settings beta, tau and p with rating as an integrity measure.
    # Again for a given game these must be consistent among all ratings and the history of each rating.
    # And change while managing leaderboards should trigger an update request for ratings relating to this game.
    trueskill_beta = models.FloatField('TrueSkill Skill Factor (ß)', default=trueskill.BETA)
    trueskill_tau = models.FloatField('TrueSkill Dynamics Factor (τ)', default=trueskill.TAU)
    trueskill_p = models.FloatField('TrueSkill Draw Probability (p)', default=trueskill.DRAW_PROBABILITY)

    @property
    def last_play_local(self):
        return self.last_play.astimezone(safe_tz(self.last_play_tz))

    @property
    def last_victory_local(self):
        return self.last_victory.astimezone(safe_tz(self.last_victory_tz))

    def __unicode__(self): return  f'{self.player} - {self.game} - {self.trueskill_eta:.1f} teeth'

    def __str__(self): return self.__unicode__()

    def __verbose_str__(self):
        return  f'{self.player} - {self.game} - {self.trueskill_eta:.1f} teeth, from (µ={self.trueskill_mu:.1f}, σ={self.trueskill_sigma:.1f} after {self.plays} plays)'

    def __rich_str__(self, link=None):
        return  f'{field_render(self.player, link)} - {field_render(self.game, link)} - {self.trueskill_eta:.1f} teeth, from (µ={self.trueskill_mu:.1f}, σ={self.trueskill_sigma:.1f} after {self.plays} plays)'

    def __detail_str__(self, link=None):
        return self.__rich_str__(link)

    class Meta(AdminModel.Meta):
        ordering = ['-trueskill_eta']
        abstract = True


class Rating(RatingModel):
    '''
    This is the actual repository of ratings that describe leaderboards.

    Its partner BackupRating stores a backup (form before the last rebuild)
    '''

    @property
    def last_performance(self) -> 'Performance':
        '''
        Returns the latest performance object that this player played this game in.
        '''
        game = self.game
        player = self.player

        plays = Session.objects.filter(Q(game=game) & (Q(ranks__player=player) | Q(ranks__team__players=player))).order_by('-date_time')

        return None if (plays is None or plays.count() == 0) else plays[0].performance(player)

    @property
    def last_winning_performance(self) -> 'Performance':
        '''
        Returns the latest performance object that this player played this game in and won.
        '''
        game = self.game
        player = self.player

        wins = Session.objects.filter(Q(game=game) & Q(ranks__rank=1) & (Q(ranks__player=player) | Q(ranks__team__players=player))).order_by('-date_time')

        return None if (wins is None or wins.count() == 0) else wins[0].performance(player)

    @property
    def link_internal(self) -> str:
        return reverse('view', kwargs={"model":self._meta.model.__name__, "pk": self.pk})

    def reset(self, session=None):
        '''
        Given a session, resets this rating object to what it was after this session.

        If no session is specified us the last played session (of that player at that game)

        Allows for a rewind of the rating to what it was at some time in past, so that
        it can be rebuilt from that point onward if desired, and/or can be used to ensure
        that all the rating related data is up to date (as of last session). Remembering
        that Rating is just a rapid access version of what is stored in Performance
        objects (each rating sits in one Performance object somewhere, being the latest
        Performance for a given player at a given game.

        :param session: A Session object (optional)
        '''
        if not isinstance(session, Session):
            session = self.player.last_play(self.game)

        if session:
            performance = session.performance(self.player)

            self.plays = performance.play_number
            self.victories = performance.victory_count

            self.last_play = session.date_time

            if performance.is_victory:
                self.last_victory = session.date_time
            else:
                last_victory = session.previous_victory(performance.player)
                self.last_victory = NEVER if last_victory is None else last_victory.session.date_time

            self.trueskill_mu = performance.trueskill_mu_after
            self.trueskill_sigma = performance.trueskill_sigma_after
            self.trueskill_eta = performance.trueskill_eta_after

            self.trueskill_mu0 = performance.trueskill_mu0
            self.trueskill_sigma0 = performance.trueskill_sigma0
            self.trueskill_beta = performance.trueskill_beta
            self.trueskill_delta = performance.trueskill_delta

            self.trueskill_tau = performance.trueskill_tau
            self.trueskill_p = performance.trueskill_p
        else:
            # If we have no session it means this player never played this game
            # So we just create new rating (get does that if no rating exists).
            Rating.get(self.player, self.game)

    @classmethod
    def create(cls, player, game, mu=None, sigma=None):
        '''
        Create a new Rating for player at game, with specified mu and sigma.

        An explicit method, rather than override of __init__ which is called
        whenever and object is instantiated which can be when creating a new
        Rating or when fetching an old one from tthe database. So not appropriate
        to override it for new Ratings.
        '''

        TS = TrueskillSettings()

        trueskill_mu = TS.mu0 if mu == None else mu
        trueskill_sigma = TS.sigma0 if sigma == None else sigma

        self = cls(player=player,
                    game=game,
                    plays=0,
                    victories=0,
                    last_play=NEVER,
                    last_victory=NEVER,
                    trueskill_mu=trueskill_mu,
                    trueskill_sigma=trueskill_sigma,
                    trueskill_eta=trueskill_mu - TS.mu0 / TS.sigma0 * trueskill_sigma,  # µ − (µ0 ÷ σ0) × σ
                    trueskill_mu0=TS.mu0,
                    trueskill_sigma0=TS.sigma0,
                    trueskill_beta=game.trueskill_beta,
                    trueskill_delta=TS.delta,
                    trueskill_tau=game.trueskill_tau,
                    trueskill_p=game.trueskill_p
                    )
        return self

    @classmethod
    def get(cls, player, game):
        '''
        Fetch (or create fromd efaults) the rating for a given player at a game
        and perform some quick data integrity checks in the process.

        :param player: a Player object
        :param game:   a Game object
        '''
        TS = TrueskillSettings()

        try:
            r = Rating.objects.get(player=player, game=game)
        except ObjectDoesNotExist:
            r = Rating.create(player=player, game=game)
        except MultipleObjectsReturned:
            raise IntegrityError("Integrity error: more than one rating for {} at {}".format(player.name_nickname, game.name))

        if not (isclose(r.trueskill_mu0, TS.mu0, abs_tol=FLOAT_TOLERANCE)
         and isclose(r.trueskill_sigma0, TS.sigma0, abs_tol=FLOAT_TOLERANCE)
         and isclose(r.trueskill_delta, TS.delta, abs_tol=FLOAT_TOLERANCE)
         and isclose(r.trueskill_beta, game.trueskill_beta, abs_tol=FLOAT_TOLERANCE)
         and isclose(r.trueskill_tau, game.trueskill_tau, abs_tol=FLOAT_TOLERANCE)
         and isclose(r.trueskill_p, game.trueskill_p, abs_tol=FLOAT_TOLERANCE)):
            SettingsWere = "µ0: {}, σ0: {}, ß: {}, δ: {}, τ: {}, p: {}".format(r.trueskill_mu0, r.trueskill_sigma0, r.trueskill_delta, r.trueskill_beta, r.trueskill_tau, r.trueskill_p)
            SettingsAre = "µ0: {}, σ0: {}, ß: {}, δ: {}, τ: {}, p: {}".format(TS.mu0, TS.sigma0, TS.delta, game.trueskill_beta, game.trueskill_tau, game.trueskill_p)
            raise DataError("Data error: A trueskill setting has changed since the last rating was saved. They were ({}) and now are ({})".format(SettingsWere, SettingsAre))
            # TODO: Issue warning to the registrar more cleanly than this
            # Email admins with notification and suggested action (fixing settings or rebuilding ratings).
            # If only game specific settings changed on that game is impacted of course.
            # If global settings are changed all ratings are affected.

        return r

    @classmethod
    def leaderboards(cls, games, style=LB_PLAYER_LIST_STYLE.none) -> dict:
        '''
        returns Game.leaderboard(style) for each game supplied in a dict keyed on game.pk

        Specifically to support Rating.rebuild really. Game handles the leaderboard
        presentations otherwise.

        :param games: a list of QuerySet of Game instances
        '''
        return {g.pk: g.leaderboard(style=style) for g in games}

    @classmethod
    def update(cls, session):
        '''
        Update the ratings for all the players of a given session.

        :param session:   A Session object
        '''
        TS = TrueskillSettings()

        # Check to see if this is the latest play for each player
        # And capture the current rating for each player (which we will update)
        is_latest = {}
        player_rating = {}
        for performance in session.performances.all():
            rating = Rating.get(performance.player, session.game)  # Create a new rating if needed
            player_rating[performance.player] = rating
            is_latest[performance.player] = session.date_time >= rating.last_play

#         log.debug(f"Update rating for session {session.id}: {session}.")
#         log.debug(f"Is latest session of {session.game} for {[k for (k,v) in is_latest.items() if v]}")
#         log.debug(f"Is not latest session of {session.game} for {[k for (k,v) in is_latest.items() if not v]}")

        # Trickle admin bypass down
        if cls.__bypass_admin__:
            session.__bypass_admin__ = True

        # Get the session impact (records results in database Performance objects)
        # This updates the Performance objects associated with that session.
        impact = session.calculate_trueskill_impacts()

        # Update the rating for this player/game combo
        # So this regardless of the sessions status as latest for any players
        # Here is not where we make that call and this method is called by rebuild()
        # which is itself called by a function that decides on the consequence
        # of so doing (whether a rebuild is needed because of future sessions,
        # relative this one).
        for player in impact:
            # Update the rating of course, only if this session is the latest in that game for this player
            # If it's not, a rebuild shoudl be triggered anyhow and the last session to update ratings during
            # that rebuild will be the latest.
            if is_latest[player]:
                r = player_rating[player]

                # Record the new rating data
                r.trueskill_mu = impact[player]["after"]["mu"]
                r.trueskill_sigma = impact[player]["after"]["sigma"]
                r.trueskill_eta = r.trueskill_mu - TS.mu0 / TS.sigma0 * r.trueskill_sigma  # µ − (µ0 ÷ σ0) × σ

                # Record the TruesSkill settings used to get them
                r.trueskill_mu0 = TS.mu0
                r.trueskill_sigma0 = TS.sigma0
                r.trueskill_delta = TS.delta
                r.trueskill_beta = session.game.trueskill_beta
                r.trueskill_tau = session.game.trueskill_tau
                r.trueskill_p = session.game.trueskill_p

                # Record the context of the rating
                r.plays = impact[player]["plays"]
                r.victories = impact[player]["victories"]

                r.last_play = session.date_time
                if session.performance(player).is_victory:
                    r.last_victory = session.date_time
                # else leave r.last_victory unchanged

                # Trickle admin bypass down
                if cls.__bypass_admin__:
                    r.__bypass_admin__ = True

                r.save()

    @classmethod
    def rebuild(cls, Game=None, From=None, Sessions=None, Reason=None, Trigger=None, Session=None):
        '''
        Rebuild the ratings for a specific game from a specific time.

        Returns a RebuildLog instance (with an html attribute if not triggered by a session)

        If neither Game nor From nor Sessions are specified, rebuilds ALL ratings
        If both Game and From specified rebuilds ratings only for that game for sessions from that datetime
        If only Game is specified, rebuilds all ratings for that game
        If only From is specified rebuilds ratings for all games from that datetime
        If only Sessions is specified rebuilds only the nominated Sessions

        :param Game:     A Game object
        :param From:     A datetime
        :param Sessions: A list of Session objects or a QuerySet of Sessions.
        :param Reason:   A string, to log as a reason for the rebuild
        :param Trigger:  A RATING_REBUILD_TRIGGER value
        :param Session:  A Session object if an edit (create or update) of a session triggered this rebuild
        '''
        # If ever performed keep a record of duration overall and per
        # session to permit a cost estimate should it happen again.
        # On a large database this could be a costly exercise, causing
        # some down time to the server (must either lock server to do
        # this as we cannot have new ratings being created while
        # rebuilding or we could have the update method above check
        # if a rebuild is underway and if so schedule an update ro

        # Bypass admin fields updates for a rating rebuild
        cls.__bypass_admin__ = True

        # First we collect the sessions that need rebuilding, they are either
        # explicity provided or implied by specifying a Game and/or From time.
        if Sessions:
            assert not Game and not From, "Invalid ratings rebuild requested."
            sessions = sorted(Sessions, key=lambda s: s.date_time)
            first_session = sessions[0]
        elif not Game and not From:
            if settings.DEBUG:
                log.debug(f"Rebuilding ALL leaderboard ratings.")

            sessions = Session.objects.all().order_by('date_time')
            first_session = sessions.first()
        else:
            if settings.DEBUG:
                log.debug(f"Rebuilding leaderboard ratings for {getattr(Game, 'name', None)} from {From}")

            sfilterg = Q(game=Game) if Game else Q()
            sfilterf = Q(date_time__gte=From) if isinstance(From, datetime) else Q()

            sessions = Session.objects.filter(sfilterg & sfilterf).order_by('date_time')
            first_session = sessions.first()

        affected_games = set([s.game for s in sessions])
        if settings.DEBUG:
            log.debug(f"{len(sessions)} Sessions to process, affecting {len(affected_games)} games.")

        # If Game isn't specified, and a list of Sessions is, then if the sessions all relate
        # to the same game log that game.
        if not Game and len(affected_games) == 1:
            Game = list(affected_games)[0]

        # We prepare a Rebuild Log entry
        rlog = RebuildLog(game=Game,
                          date_time_from=first_session.date_time_local,
                          ratings=len(sessions),
                          reason=Reason)

        # Record what triggered the rebuild
        if not Trigger is None:
            rlog.trigger = Trigger.value
            if not Session is None:
                rlog.session = Session

        # Need to save it to get a PK before we can attach the sessions set to the log entry.
        rlog.save()
        rlog.sessions.set(sessions)

        # Start the timer
        start = localtime()

        # Now save the leaderboards for all affected games.
        rlog.save_leaderboards(affected_games, "before")

        # Delete all BackupRating objects
        BackupRating.reset()

        # Traverse sessions in chronological order (order_by is the time of the session) and update ratings from each session
        ratings_to_reset = set()  # Use a set to avoid duplicity
        backedup = set()
        for s in sessions:
            # Backup a rating only the first time we encounter it
            # Ratings apply to a player/game pair and we want a backup
            # of the rating before this rebuild process starts to
            # compare the final ratings to. We only want to backup
            # ratings that are being updated though hence first time
            # see a player/game pair in the rebuild process, nab a
            # backup.
            for p in s.performances.all():
                rkey = (p.player, s.game)
                if not rkey in backedup:
                    try:
                        rating = Rating.get(p.player, s.game)
                        BackupRating.clone(rating)
                    except:
                        # Ignore errors, We just won't record that rating as backedup.
                        pass
                    else:
                        backedup.add(rkey)

            cls.update(s)
            for p in s.players:
                ratings_to_reset.add((p, s.game))  # Collect a set of player, game tuples.

        # After having updated all the sessions we need to ensure
        # that the Rating objects are up to date.
        for rating in ratings_to_reset:
            if settings.DEBUG:
                log.debug(f"Resetting rating for {rating}")
            r = Rating.get(*rating)  # Unpack the tuple to player, game
            r.reset()  # Sets the rating to that after the ast played session in that game/player that the rating is for
            r.save()

        # Desist from bypassing admin field updates
        cls.__bypass_admin__ = False

        # Now save the leaderboards for all affected games again!.
        rlog.save_leaderboards(affected_games, "after")

        # Stop the timer and record the duration
        end = localtime()
        rlog.duration = end - start

        # And save the complete Rebuild Log entry
        rlog.save()

        if Trigger == RATING_REBUILD_TRIGGER.user_request:
            if settings.DEBUG:
                log.debug("Generating HTML diff.")

            # Add an html attribute to rlog (not a database field) so that the caller can render a report.
            rlog.html = BackupRating.html_diff()

        if settings.DEBUG:
            log.debug("Done.")

        return rlog

    @classmethod
    def estimate_rebuild_cost(cls, n=1):
        '''
        Uses the rebuild logs to estimate the cost of rebuilding.

        :param n: the number of sessions we'll rebuild ratings for.
        '''
        Cost = ExpressionWrapper(F('duration') / F('ratings'), output_field=models.DurationField())
        Costs = RebuildLog.objects.all().annotate(cost=Cost).values_list('cost', flat=True)

        if Costs:
            costs = [c.total_seconds() for c in Costs]
            mean_cost = mean(costs)
            nstdev_cost = stdev(costs) / mean_cost

            cost_estimate = timedelta(seconds=n * mean_cost)  # The predicted cost of prebuilding n sessions
            cost_variance = timedelta(seconds=nstdev_cost)  # The coeeficient of variance (0 to 1)

            return cost_estimate, cost_variance
        else:
            return None

    def check_integrity(self, passthru=True):
        '''
        Perform integrity check on this rating record
        '''
        L = AssertLog(passthru)

        pfx = f"Rating Integrity error (id: {self.id}):"

        # Check for uniqueness
        same = Rating.objects.filter(player=self.player, game=self.game)
        L.Assert(same.count() <= 1, f"{pfx} Duplicate rating entries for player: {self.player} and game: {self.game}. Dupes are {[s.id for s in same]}")

        # Check that rating matches last performance
        last_play = self.last_performance
        last_win = self.last_winning_performance

        L.Assert(not last_play is None, f"{pfx} Has no Last Play!")

        if last_play:
            L.Assert(isclose(self.trueskill_mu, last_play.trueskill_mu_after, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance µ mismatch. Rating has {self.trueskill_mu} Last Play has {last_play.trueskill_mu_after}.")
            L.Assert(isclose(self.trueskill_sigma, last_play.trueskill_sigma_after, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance σ mismatch. Rating has {self.trueskill_sigma} Last Play has {last_play.trueskill_sigma_after}.")
            L.Assert(isclose(self.trueskill_eta, last_play.trueskill_eta_after, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance η mismatch. Rating has {self.trueskill_eta} Last Play has {last_play.trueskill_eta_after}.")

            L.Assert(isclose(self.trueskill_mu0, last_play.trueskill_mu0, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance µ0 mismatch. Rating has {self.trueskill_mu0} Last Play has {last_play.trueskill_mu0}.")
            L.Assert(isclose(self.trueskill_sigma0, last_play.trueskill_sigma0, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance σ0 mismatch. Rating has {self.trueskill_sigma0} Last Play has {last_play.trueskill_sigma0}.")
            L.Assert(isclose(self.trueskill_delta, last_play.trueskill_delta, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance δ mismatch. Rating has {self.trueskill_delta} Last Play has {last_play.trueskill_delta}.")

            L.Assert(isclose(self.trueskill_tau, last_play.trueskill_tau, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance τ mismatch. Rating has {self.trueskill_tau} Last Play has {last_play.trueskill_tau}.")
            L.Assert(isclose(self.trueskill_p, last_play.trueskill_p, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance p mismatch. Rating has {self.trueskill_p} Last Play has {last_play.trueskill_p}.")

            # Check that the play and victory counts reflect what Performance says
            L.Assert(self.plays == last_play.play_number, f"{pfx} Play count mismatch. Rating has {self.plays} Last play has {last_play.play_number}.")
            L.Assert(self.victories == last_play.victory_count, f"{pfx} Victory count mismatch. Rating has {self.victories} Last play has {last_play.victory_count}.")

            # Check that last_play and last_victory dates are accurate reflections on Performance records
            L.Assert(self.last_play == last_play.session.date_time, f"{pfx} Last play mismatch. Rating has {self.last_play} Last play has {last_play.session.date_time}.")

        if last_win:
            L.Assert(self.last_victory == last_win.session.date_time, f"{pfx} Last victory mismatch. Rating has {self.last_victory} Last victory has {last_win.session.date_time}.")
        else:
            L.Assert(self.last_victory == NEVER, f"{pfx} Last victory mismatch. Rating has {self.last_victory} when expecting the NEVER value of {NEVER}.")

        return L.assertion_failures

    def clean(self):
        # TODO: diagnose when this is called. What can we assume about session cleans? And what not?
        # This rating must be unique
        same = Rating.objects.filter(player=self.player, game=self.game)

        if same.count() > 1:
            raise ValidationError("Duplicate ratings found for player: {} and game: {}".format(self.player, self.game))

        # Rating should match the last performance
        # TODO: When do we land here? And how do we sync with self.update?

    class Meta(AdminModel.Meta):
        ordering = ['-trueskill_eta']
        verbose_name = "Rating"
        verbose_name_plural = "Ratings"


class BackupRating(RatingModel):
    '''
    A simple container for a complete backup of Rating.

    Used when doing a rebuild of ratings so as to have the previous copy on hand, and to be able to
    compare to see what the impact of the rebuild was. This can be very relevant if rebuilding because of
    a change to TrueSkill settings for example, when tuning the settings for particular games.

    # TODO: Put an option on the leaderboards view to see the Backup leaderboards, and another to show a comparison
    '''

    @classmethod
    def reset(cls):
        '''
        Deletes all backup ratings
        '''
        cls.objects.all().delete()

    @classmethod
    def clone(cls, rating):
        '''
        Clones a rating into the Backup model (database table)

        :param rating: A Rating object
        '''
        try:
            backup = cls.objects.get(player=rating.player, game=rating.game)
        except ObjectDoesNotExist:
            backup = cls()

        for field in rating._meta.fields:
            if not field.primary_key:
                setattr(backup, field.name, getattr(rating, field.name))

        backup.save()

    @classmethod
    def diff(cls, show_unchanged=True):
        '''
        Returns an a dictionary summarising differences between current ratings and
        backed up ratings. It is an ordered dictionary keyed on the tuple of player id
        and game id containing a 9 tuple of the old, diff and new values for the three
        rating measures (eta, mu, sigma)

        :param show_unchanged: Include ratings that didn't change.
        '''
        diffs = OrderedDict()
        for r in cls.objects.all().order_by('game', '-trueskill_eta'):
            R = Rating.get(r.player, r.game)

            diff_eta = R.trueskill_eta - r.trueskill_eta
            diff_mu = R.trueskill_mu - r.trueskill_mu
            diff_sigma = R.trueskill_sigma - r.trueskill_sigma

            if (show_unchanged
                or abs(diff_eta) > FLOAT_TOLERANCE
                or abs(diff_mu) > FLOAT_TOLERANCE
                or abs(diff_sigma) > FLOAT_TOLERANCE):
                diffs[(r.player.pk, r.game.pk)] = (r.trueskill_eta, r.trueskill_mu, r.trueskill_sigma,
                                                   diff_eta, diff_mu, diff_sigma,
                                                   R.trueskill_eta, R.trueskill_mu, R.trueskill_sigma)

        return diffs

    @classmethod
    def html_diff(cls, show_unchanged=True):
        '''
        Returns an HTML table (string) summarising differences between current ratings and
        backed up ratings.

        :param show_unchanged: Include ratings that didn't change.
        '''
        sign = lambda a: '-' if a < 0 else '+'

        diffs = cls.diff(show_unchanged)

        html = "<TABLE>"
        html += "<TR>"
        html += "<TH>Game</TH>"
        html += "<TH>Player</TH>"
        html += "<TH>Rating (η)</TH>"
        html += "<TH>Mean (µ)</TH>"
        html += "<TH>Standard Deviation(µ)</TH>"
        html += "</TR>"
        for r in cls.objects.all().order_by('game', '-trueskill_eta'):
            (old_eta, old_mu, old_sigma,
             diff_eta, diff_mu, diff_sigma,
             new_eta, new_mu, new_sigma) = diffs[(r.player.pk, r.game.pk)]

            eqn_eta = f"{old_eta:.4f} {sign(diff_eta)} {abs(diff_eta):.4f} = {new_eta:.4f}"
            eqn_mu = f"{old_mu:.4f} {sign(diff_mu)} {abs(diff_mu):.4f} = {new_mu:.4f}"
            eqn_sigma = f"{old_sigma:.4f} {sign(diff_sigma)} {abs(diff_sigma):.4f} = {new_sigma:.4f}"

            if (show_unchanged
                or abs(diff_eta) > FLOAT_TOLERANCE
                or abs(diff_mu) > FLOAT_TOLERANCE
                or abs(diff_sigma) > FLOAT_TOLERANCE):
                html += "<TR>"
                html += f"<TD>{r.game.name}</TD>"
                html += f"<TD>{r.player.name()}</TD>"
                html += f"<TD>{eqn_eta}</TD>"
                html += f"<TD>{eqn_mu}</TD>"
                html += f"<TD>{eqn_sigma}</TD>"
                html += "</TR>"
        html += "</TABLE>"

        return html


class League(AdminModel):
    '''
    A group of Players who are competing at Games which have a Leaderboard of Ratings.

    Leagues operate independently of one another, meaning that
    when Sessions are recorded, only the Locations, Players and Games will appear on selectors.

    All Leagues share the same global and game Trueskill settings, so that a
    meaningful global leaderboard can be reported for any game across all leagues.
    '''
    name = models.CharField('Name of the League', max_length=MAX_NAME_LENGTH, validators=[RegexValidator(regex=f'^{ALL_LEAGUES}$', message=f'{ALL_LEAGUES} is a reserved league name', code='reserved', inverse_match=True)])

    manager = models.ForeignKey('Player', verbose_name='Manager', related_name='leagues_managed', null=True, on_delete=models.SET_NULL)  # if the manager is deletect, keep the league!

    locations = models.ManyToManyField('Location', verbose_name='Locations', blank=True, related_name='leagues_playing_here')
    players = models.ManyToManyField('Player', verbose_name='Players', blank=True, related_name='member_of_leagues')
    games = models.ManyToManyField('Game', verbose_name='Games', blank=True, related_name='played_by_leagues')

    @property
    def link_internal(self) -> str:
        return reverse('view', kwargs={"model":self._meta.model.__name__, "pk": self.pk})

    @property
    def leaderboards(self) -> list:
        '''
        The leaderboards for this league.

        Returns a dictionary of leaderards keyed on game.
        '''
        return self.leaderboard()

    @property_method
    def leaderboard(self, game=None, asat=None, style=LB_PLAYER_LIST_STYLE.none) -> tuple:
        '''
        Return a leaderboard for a specified game or if no game is provided, a dictionary of such
        lists keyed on game.
        '''
        if game is None:
            lb = {}
            games = Game.objects.filter(leagues=self)
            for game in games:
                lb[game] = self.leaderboard(game)
        else:
            lb = game.leaderboard(leagues=self, asat=asat, style=style)

        return lb

    selector_field = "name"

    @classmethod
    def selector_queryset(cls, query="", session={}, all=False):
        '''
        Provides a queryset for ModelChoiceFields (select widgets) that ask for it.
        :param cls: Our class (so we can build a queryset on it to return)
        :param query: A simple string being a query that is submitted (typically typed into a django-autcomplete-light ModelSelect2 or ModelSelect2Multiple widget)
        :param session: The request session (if there's a filter recorded there we honor it)
        :param all: Requests to ignore any default league filtering
        '''
        qs = cls.objects.all()

        if not all:
            league = session.get('filter', {}).get('league', None)
            if league:
                # TODO: It's a bit odd to filter leagues on league. Might consider instead to filter
                #       on related leagues, that is the more complex question, for a selected league,
                #       itself and all leagues that share one or players with this league. These are
                #       conceivably related fields.
                qs = qs.filter(pk=league)

        if query:
            qs = qs.filter(**{f'{cls.selector_field}__istartswith': query})

        return qs

    add_related = None

    def __unicode__(self): return getattr(self, self.selector_field)

    def __str__(self): return self.__unicode__()

    def __verbose_str__(self):
        return u"{} (manager: {})".format(self, self.manager)

    def __rich_str__(self, link=None):
        return u"{} (manager: {})".format(field_render(self, link), field_render(self.manager, link))

    def __detail_str__(self, link=None):
        detail = self.__rich_str__(link)
        detail += "<UL>"
        for p in self.players.all():
            detail += "<LI>{}</LI>".format(field_render(p, link))
        detail += "</UL>"
        return detail

    class Meta(AdminModel.Meta):
        verbose_name = "League"
        verbose_name_plural = "Leagues"

        ordering = ['name']
        constraints = [ models.UniqueConstraint(fields=['name'], name='unique_league_name') ]


class Team(AdminModel):
    '''
    A player team, which is defined when a team play game is recorded and needed to properly display a session as it was played,
    and to calculate team based TrueSkill ratings. Teams have no names just a list of players.

    Teams may have names but don't need them.
    '''
    name = models.CharField('Name of the Team (optional)', max_length=MAX_NAME_LENGTH, null=True)
    players = models.ManyToManyField('Player', verbose_name='Players', blank=True, editable=False, related_name='member_of_teams')

    @property
    def games_played(self) -> list:
        games = []
        for r in self.ranks.all():
            game = r.session.game
            if not game in games:
                games.append(game)

        return games

    @property
    def link_internal(self) -> str:
        return reverse('view', kwargs={"model":self._meta.model.__name__, "pk": self.pk})

    add_related = ["players"]

    def __unicode__(self):
        if self.name:
            return self.name
        elif self._state.adding:  # self.players is unavailable
            return "Empty Unsaved Team"
        else:
            return u", ".join([str(p) for p in self.players.all()])

    def __str__(self): return self.__unicode__()

    def __verbose_str__(self):
        name = self.name if self.name else ""
        return name + u" (" + u", ".join([str(p) for p in self.players.all()]) + u")"

    def __rich_str__(self, link=None):
        games = self.games_played
        if len(games) > 2:
            game_str = ", ".join(map(lambda g: field_render(g, link), games[0:1])) + "..."
        elif len(games) > 0:
            game_str = ", ".join(map(lambda g: field_render(g, link), games))
        else:
            game_str = html.escape("<No Game>")

        name = field_render(self.name, link_target_url(self, link)) if self.name else ""
        return name + u" (" + u", ".join([field_render(p, link) for p in self.players.all()]) + u") for " + game_str

    def __detail_str__(self, link=None):
        if self.name:
            detail = field_render(self.name, link_target_url(self, link))
        else:
            detail = html.escape("<Nameless Team>")

        games = self.games_played
        if len(games) > 2:
            game_str = ", ".join(map(lambda g: field_render(g, link), games[0:1])) + "..."
        elif len(games) > 0:
            game_str = ", ".join(map(lambda g: field_render(g, link), games))
        else:
            game_str = html.escape("<no game>")

        detail += " for " + game_str + "<UL>"
        for p in self.players.all():
            detail += "<LI>{}</LI>".format(field_render(p, link))
        detail += "</UL>"
        return detail

    class Meta(AdminModel.Meta):
        verbose_name = "Team"
        verbose_name_plural = "Teams"


class Player(PrivacyMixIn, AdminModel):
    '''
    A player who is presumably collecting Ratings on Games and participating in leaderboards in one or more Leagues.

    Players can be Registrars, meaning they are permitted to record session results, or Staff meaning they can access the admin site.
    '''
    # Basic Player fields
    name_nickname = models.CharField('Nickname', max_length=MAX_NAME_LENGTH, unique=True)
    name_personal = models.CharField('Personal Name', max_length=MAX_NAME_LENGTH)
    name_family = models.CharField('Family Name', max_length=MAX_NAME_LENGTH)

    email_address = models.EmailField('Email Address', blank=True, null=True)
    BGGname = models.CharField('BoardGameGeek Name', max_length=MAX_NAME_LENGTH, default='', blank=True, null=True)  # BGG URL is https://boardgamegeek.com/user/BGGname

    # Privilege fields
    is_registrar = models.BooleanField('Authorised to record session results?', default=False)
    is_staff = models.BooleanField('Authorised to access the admin site?', default=False)

    # Membership fields
    teams = models.ManyToManyField('Team', through=Team.players.through, verbose_name='Teams', editable=False, related_name='players_in_team')  # Don't edit teams always inferred from Session submissions
    leagues = models.ManyToManyField('League', through=League.players.through, verbose_name='Leagues', blank=True, related_name='players_in_league')

    # A default or preferred league for each player. Optional. Can be used to customise views.
    league = models.ForeignKey(League, verbose_name='Preferred League', related_name="preferred_league_of", blank=True, null=True, default=None, on_delete=models.SET_NULL)

    # account
    user = models.OneToOneField(User, verbose_name='Username', related_name='player', blank=True, null=True, default=None, on_delete=models.SET_NULL)

    # Privacy control (interfaces with django_model_privacy_mixin)
    visibility = (
        ('all', 'Everyone'),
        ('share_leagues', 'League Members'),
        ('share_teams', 'Team Members'),
        ('all_is_registrar', 'Registrars'),
        ('all_is_staff', 'Staff'),
    )

    visibility_name_nickname = BitField(visibility, verbose_name='Nickname Visibility', default=('all',), blank=True)
    visibility_name_personal = BitField(visibility, verbose_name='Personal Name Visibility', default=('all',), blank=True)
    visibility_name_family = BitField(visibility, verbose_name='Family Name Visibility', default=('share_leagues',), blank=True)
    visibility_email_address = BitField(visibility, verbose_name='Email Address Visibility', default=('share_leagues', 'share_teams'), blank=True)
    visibility_BGGname = BitField(visibility, verbose_name='BoardGameGeek Name Visibility', default=('share_leagues', 'share_teams'), blank=True)

    @property
    def owner(self) -> User:
        return self.user

    @property
    def full_name(self) -> str:
        return "{} {}".format(self.name_personal, self.name_family)

    @property
    def complete_name(self) -> str:
        return "{} {} ({})".format(self.name_personal, self.name_family, self.name_nickname)

    @property
    def games_played(self) -> list:
        '''
        Returns all the games that that this player has played
        '''
        games = Game.objects.filter((Q(sessions__ranks__player=self) | Q(sessions__ranks__team__players=self))).distinct()
        return None if (games is None or games.count() == 0) else games

    @property
    def games_won(self) -> list:
        '''
        Returns all the games that that this player has won
        '''
        games = Game.objects.filter(Q(sessions__ranks__rank=1) & (Q(sessions__ranks__player=self) | Q(sessions__ranks__team__players=self))).distinct()
        return None if (games is None or games.count() == 0) else games

    @property_method
    def last_play(self, game=None) -> object:
        '''
        For a given game returns the session that represents the last time this player played that game.
        '''
        sFilter = (Q(ranks__player=self) | Q(ranks__team__players=self))
        if game:
            sFilter &= Q(game=game)

        plays = Session.objects.filter(sFilter).order_by('-date_time')

        return None if (plays is None or plays.count() == 0) else plays[0]

    @property_method
    def last_win(self, game=None) -> object:
        '''
        For a given game returns the session that represents the last time this player won that game.
        '''
        sFilter = Q(ranks__rank=1) & (Q(ranks__player=self) | Q(ranks__team__players=self))
        if game:
            sFilter &= Q(game=game)

        plays = Session.objects.filter(sFilter).order_by('-date_time')

        return None if (plays is None or plays.count() == 0) else plays[0]

    @property
    def last_plays(self) -> list:
        '''
        Returns the session of last play for each game played.
        '''
        sessions = {}
        played = [] if self.games_played is None else self.games_played
        for game in played:
            sessions[game] = self.last_play(game)
        return sessions

    @property
    def last_wins(self) -> list:
        '''
        Returns the session of last play for each game won.
        '''
        sessions = {}
        played = [] if self.games_played is None else self.games_played
        for game in played:
            lw = self.last_win(game)
            if not lw is None:
                sessions[game] = self.last_win(game)
        return sessions

    @property
    def leaderboard_positions(self) -> list:
        '''
        Returns a dictionary of leagues, each value being a dictionary of games with a
        value that is the leaderboard position this player holds on that league for
        that game.
        '''
        positions = {}

        played = [] if self.games_played is None else self.games_played

        # Include a GLOBAL league only if this player is in more than one league, else
        # Global is identical to their one league anyhow.
        multiple_leagues = self.leagues.all().count() > 1
        if multiple_leagues:
            positions[ALL_LEAGUES] = {}
            for game in played:
                positions[ALL_LEAGUES][game] = self.leaderboard_position(game)

        for league in self.leagues.all():
            positions[league] = {}
            for game in played:
                positions[league][game] = self.leaderboard_position(game, league)

        return positions

    @property
    def leaderboards_winning(self) -> list:
        '''
        Returns a dictionary of leagues, each value being a list of games this player
        is winning the leaderboard on.
        '''
        result = {}

        played = [] if self.games_played is None else self.games_played

        # Include a GLOBAL league only if this player is in more than one league, else
        # Global is identical to their one league anyhow.
        multiple_leagues = self.leagues.all().count() > 1
        if multiple_leagues:
            result[ALL_LEAGUES] = []
            for game in played:
                if self.is_at_top_of_leaderbard(game):
                    result[ALL_LEAGUES].append(game)

        for league in self.leagues.all():
            result[league] = []
            for game in played:
                if self.is_at_top_of_leaderbard(game, league):
                    result[league].append(game)

        return result

    @property
    def link_internal(self) -> str:
        return reverse('view', kwargs={"model":self._meta.model.__name__, "pk": self.pk})

    @property
    def link_external(self) -> str:
        if self.BGGname and not 'BGGname' in self.hidden:
            return "https://boardgamegeek.com/user/{}".format(self.BGGname)
        else:
            return None

    def name(self, style="full"):
        '''
        Renders the players name in a nominated style
        :param style: Supports "nick", "full", "complete", "flexi"

        flexi is a special request to return {pk, nick, full, complete}
        empowering the caller to choose the name style later. This is
        ideally to allow a client to choose rendering in Javascript
        rather than fixing the rendering at server side.
        '''
        # TODO: flexi has to use a delimeter that cannot be in a name and that should be enforced (names have them escaped
        #       currently using comma, but names can stil have commas!
        return (self.name_nickname if style == "nick"
           else self.full_name if style == "full"
           else self.complete_name if style == "complete"
           else f"{{{self.pk},{self.name_nickname},{self.full_name},{self.complete_name}}}" if style == "flexi"
           else "Anonymous")

    def rating(self, game):
        '''
        Returns the Trueskill rating for this player at the specified game
        '''
        try:
            r = Rating.objects.get(player=self, game=game)
        except ObjectDoesNotExist:
            r = Rating.create(player=self, game=game)
        except MultipleObjectsReturned:
            raise ValueError("Database error: more than one rating for {} at {}".format(self.name_nickname, game.name))
        return r

    def leaderboard_position(self, game, leagues=[]):
        lb = game.leaderboard(leagues, style=LB_PLAYER_LIST_STYLE.data)
        for pos, entry in enumerate(lb):
            if entry[0] == self.pk:
                return pos + 1  # pos is 0 based, leaderboard positions are 1 based

    def is_at_top_of_leaderbard(self, game, leagues=[]):
        return self.leaderboard_position(game, leagues) == 1

    selector_field = "name_nickname"

    @classmethod
    def selector_queryset(cls, query="", session={}, all=False):
        '''
        Provides a queryset for ModelChoiceFields (select widgets) that ask for it.
        :param cls: Our class (so we can build a queryset on it to return)
        :param query: A simple string being a query that is submitted (typically typed into a django-autcomplete-light ModelSelect2 or ModelSelect2Multiple widget)
        :param session: The request session (if there's a filter recorded there we honor it)
        :param all: Requests to ignore any default league filtering
        '''
        qs = cls.objects.all()

        if not all:
            league = session.get('filter', {}).get('league', None)
            if league:
                # TODO: Should really respect s['filter_priorities'] as the list view does.
                qs = qs.filter(leagues=league)

        if query:
            qs = qs.filter(**{f'{cls.selector_field}__istartswith': query})

        qs = qs.annotate(play_count=Count('performances')).order_by("-play_count")

        return qs

    add_related = None

    def __unicode__(self): return getattr(self, self.selector_field)

    def __str__(self): return self.__unicode__()

    def __verbose_str__(self):
        return u'{} {} ({})'.format(self.name_personal, self.name_family, self.name_nickname)

    def __rich_str__(self, link=None):
        return u'{} - {}'.format(field_render(self.__verbose_str__(), link_target_url(self, link)), field_render(self.email_address, link if link == flt.none else flt.mailto))

    def __detail_str__(self, link=None):
        detail = self.__rich_str__(link)

        lps = self.leaderboard_positions

        detail += "<BR>Leaderboard positions:<UL>"
        for league in lps:
            detail += "<LI>in League: {}</LI><UL>".format(field_render(league, link))
            for game in lps[league]:
                detail += "<LI>{}: {}</LI>".format(field_render(game, link), lps[league][game])
            detail += "</UL>"
        detail += "</UL>"

        return detail

    # TODO: clean() method to force test that player is in a league!
    class Meta(AdminModel.Meta):
        verbose_name = "Player"
        verbose_name_plural = "Players"
        ordering = ['name_nickname']


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    formfield_overrides = { BitField: {'widget': BitFieldCheckboxSelectMultiple}, }


DEFAULT_TOURNEY_MIN_PLAYS = 2
DEFAULT_TOURNEY_WEIGHT = 1
DEFAULT_TOURNEY_ALLOWED_IMBALANCE = 0.5


class TourneyRules(AdminModel):
    '''
    A custom Through table for Tourney-Games that specifies the rules that apply to a given game in a given tourney
    especially the a weight for each game and a minimim play count for each game.

    The weight is used in building an aggregate rating by summing weighted mus, and summming weighted sigmas.

    Weights should be normalised in such an application.
    '''
    tourney = models.ForeignKey('Tourney', related_name="rules", on_delete=models.CASCADE)  # If the tourney is deleted delete this rule
    game = models.ForeignKey('Game', on_delete=models.CASCADE)  # If the game is deleted delete this rule

    # Require a minimum number of plays inhtis game to rank the tourney
    min_plays = models.PositiveIntegerField('Minimum number of plays to rank in tourney', default=DEFAULT_TOURNEY_MIN_PLAYS)

    # Skill weight for this game (shoudl be normalised across all games in thee tourney when used)
    weight = models.FloatField('The weighting of this games contribution to a Tourney rating', default=DEFAULT_TOURNEY_WEIGHT)

    @property
    def link_internal(self) -> str:
        return reverse('view', kwargs={"model":self._meta.model.__name__, "pk": self.pk})

    def __unicode__(self):
        return f'{self.tourney.name} - {self.game.name}'

    def __str__(self): return self.__unicode__()

    def __verbose_str__(self):
        return f'{self.tourney.name} - {self.game.name}: min_plays={self.min_plays}, weight={self.weight}'

    def __rich_str__(self, link=None):
        tourney_name = field_render(self.tourney.name, link_target_url(self.tourney, link))
        game_name = field_render(self.game.name, link_target_url(self.game, link))
        return f'{tourney_name} - {game_name}: min_plays={self.min_plays}, weight={self.weight}'

    def __detail_str__(self, link=None):
        return self.__rich_str__(link)

    class Meta(AdminModel.Meta):
        verbose_name = "Rule"
        verbose_name_plural = "Rules"


class Tourney(AdminModel):
    '''A Tourney is simply a group of games that can present a shared leaderboard according to specified weights.'''
    name = models.CharField('Name of the Tourney', max_length=200)
    games = models.ManyToManyField('Game', verbose_name='Games', through=TourneyRules)

    # Require a certain play balance among the tourney games.
    # This is the coefficient of variation between play counts (for all game sin the tourney by a given player)
    # 0 requires that all games be played the same number of times.
    # 1 is extremely tolerant, allowing the the mean value between them
    #    for a two game tourney allows all play imblance
    #    for a many game tourney not quite guaranteed (outliers may be ecluded still)
    # See: https://en.wikipedia.org/wiki/Coefficient_of_variation
    allowed_imbalance = models.FloatField('Maximum play count imbalance to rank in tourney', default=DEFAULT_TOURNEY_ALLOWED_IMBALANCE)

    @property
    def players(self) -> set:
        '''
        Return a QuerySet of players who qualify for this tournament.
        '''
        # First collect the tourney rules
        min_plays = {}
        for r in self.rules.all():
            min_plays[r.game] = r.min_plays

        # Collect a list of player sets (one that meats each rule)
        players = []
        for g in self.games.all():
            if g in min_plays:
                players.append(set(Performance.objects.filter(session__game=g).order_by().values_list('player').annotate(playcount=Count('player')).filter(playcount__gte=min_plays[g]).values_list('player', flat=True)))

        # Find the intersection
        players = set.intersection(*players)

        # Get the players
        return Player.objects.filter(pk__in=players)

    def gameplays_needed(self, player) -> dict:
        '''
        For a given player returns a dict keyed on Game which has an integer count stating the number
        of times they need to play those games to qualify for this tourney.

        :param player: an instance of Player
        '''
        pass

    @property
    def link_internal(self) -> str:
        return reverse('view', kwargs={"model":self._meta.model.__name__, "pk": self.pk})

    def __unicode__(self):
        return self.name

    def __str__(self): return self.__unicode__()

    def __verbose_str__(self):
        games = ", ".join([g.name for g in self.games.all()])
        return f'{self.name} - {games}'

    def __rich_str__(self, link=None):
        name = field_render(self.name, link_target_url(self, link))
        games = ", ".join([field_render(g.name, link_target_url(g, link)) for g in self.games.all()])
        return f'{name} - {games}'

    def __detail_str__(self, link=None):
        rules = {}
        for r in self.rules.all():
            rules[r.game] = r

        game_detail = {}
        for g in self.games.all():
            if g in rules:
                game_detail[g] = field_render(g.name, link_target_url(g, link)) + f": min_plays={rules[g].min_plays}, weight={rules[g].weight}"
            else:
                game_detail[g] = field_render(g.name, link_target_url(g, link)) + "Error: rules are missing!"

        name = field_render(self.name, link_target_url(self, link))
        games = "</li><li>".join([game_detail[g] for g in self.games.all()])
        games = f"<ul><li>{games}</li></ul"
        return f'{name}:{games}'

    class Meta(AdminModel.Meta):
        verbose_name = "Tourney"
        verbose_name_plural = "Tourneys"
        ordering = ['name']
        constraints = [ models.UniqueConstraint(fields=['name'], name='unique_tourney_name') ]


class Game(AdminModel):
    '''A game that Players can be Rated on and which has a Leaderboard (global and per League). Defines Game specific Trueskill settings.'''
    BGGid = models.PositiveIntegerField('BoardGameGeek ID')  # BGG URL is https://boardgamegeek.com/boardgame/BGGid
    name = models.CharField('Name of the Game', max_length=200)

    # Which play modes the game supports. This will decide the formats the session submission form supports
    individual_play = models.BooleanField('Supports individual play', default=True)
    team_play = models.BooleanField('Supports team play', default=False)

    # Game scoring options
    # Game scores are not used by TrueSkill but can be used for ranking implicitly
    NO_SCORES = 0
    INDIVIDUAL_HIGH_SCORE_WINS = 1
    INDIVIDUAL_LOW_SCORE_WINS = 2
    TEAM_HIGH_SCORE_WINS = 3
    TEAM_LOW_SCORE_WINS = 4
    ScoringChoices = (
        (NO_SCORES, 'No scores'),
        ('Individual Scores', (
            (INDIVIDUAL_HIGH_SCORE_WINS, 'High Score wins'),
            (INDIVIDUAL_LOW_SCORE_WINS, 'Low Score wins'),
        )),
        ('Team Scores', (
            (TEAM_HIGH_SCORE_WINS, 'High score wins'),
            (TEAM_LOW_SCORE_WINS, 'Low score wins'),
        ))
    )
    scoring = models.PositiveSmallIntegerField(choices=ScoringChoices, default=NO_SCORES, blank=False)

    # Player counts, also inform the session logging form how to render
    min_players = models.PositiveIntegerField('Minimum number of players', default=2)
    max_players = models.PositiveIntegerField('Maximum number of players', default=4)

    min_players_per_team = models.PositiveIntegerField('Minimum number of players in a team', default=2)
    max_players_per_team = models.PositiveIntegerField('Maximum number of players in a team', default=4)

    # Can be used to offer suggested session times in forms when entering a series of results.
    # (i.e. last game time plus this games expected play time).
    expected_play_time = models.DurationField('Expected play time', default=timedelta(minutes=90))

    # Which leagues play this game? A way to keep the game selector focussed on games a given league actually plays.
    leagues = models.ManyToManyField('League', verbose_name='Leagues', blank=True, related_name='games_played', through=League.games.through)

    # Which tourneys (if any) is this game a part of?
    tourneys = models.ManyToManyField('Tourney', verbose_name='Tourneys', blank=True, through=TourneyRules)

    # Game specific TrueSkill settings
    # tau: 0- describes the luck element in a game.
    #      0 is a game of pure skill,
    #        there is no upper limit. It is added to sigma after each re-reating (game session recorded)
    # p  : 0-1 describes the probability of a draw. It affects how far the mu of drawn players move toward
    #        each other when a draw is recorded after each rating.
    #      0 means lots
    #      1 means not at all
    trueskill_beta = models.FloatField('TrueSkill Skill Factor (ß)', default=trueskill.BETA)
    trueskill_tau = models.FloatField('TrueSkill Dynamics Factor (τ)', default=trueskill.TAU)
    trueskill_p = models.FloatField('TrueSkill Draw Probability (p)', default=trueskill.DRAW_PROBABILITY)

    @property
    def global_sessions(self) -> list:
        '''
        Returns a list of sessions that played this game. Across all leagues.
        '''
        return self.session_list()

    @property
    def league_sessions(self) -> dict:
        '''
        Returns a dictionary keyed on league, with a list of sessions that played this game as the value.
        '''
        leagues = League.objects.all()
        sl = {}
        sl[ALL_LEAGUES] = self.session_list()
        for league in leagues:
            sl[league] = self.session_list(league)
        return sl

    @property
    def global_plays(self) -> dict:
        return self.play_counts()

    @property
    def league_plays(self) -> dict:
        '''
        Returns a dictionary keyed on league, of:
        the number of plays this game has experienced, as a dictionary containing:
            total: is the sum of all the individual player counts (so a count of total play experiences)
            max: is the largest play count of any player
            average: is the average play count of all players who've played at least once
            players: is a count of players who played this game at least once
            session: is a count of the number of sessions this game has been played
        '''
        leagues = League.objects.all()
        pc = {}
        pc[ALL_LEAGUES] = self.play_counts()
        for league in leagues:
            pc[league] = self.play_counts(league)
        return pc

    @property
    def league_leaderboards(self) -> dict:
        '''
        The leaderboards for this game as a dictionary keyed on league
        with the special ALL_LEAGUES holding the global leaderboard.

        Each leaderboard is an ordered list of (player,rating, plays) tuples
        for the league.
        '''
        leagues = League.objects.all()
        lb = {}
        lb[ALL_LEAGUES] = self.leaderboard()
        for league in leagues:
            lb[league] = self.leaderboard(league)
        return lb

    @property
    def global_leaderboard(self) -> list:
        '''
        The leaderboard for this game considering all leagues together, as a simple property of the game.

        Returns as an ordered list of (player,rating, plays) tuples

        The leaderboard for a specific league is available through the leaderboard method.
        '''
        return self.leaderboard()

#     @property
#     def global_leaderboard2(self) -> dict:
#         '''
#         Should be same as global_leaderboards but by another means. A test of the query only
#         '''
#         leagues = list(League.objects.all().values_list('pk', flat=True))
#         return self.leaderboard(leagues)

    @property
    def link_internal(self) -> str:
        return reverse('view', kwargs={"model":self._meta.model.__name__, "pk": self.pk})

    @property
    def link_external(self) -> list:
        if self.BGGid:
            return "https://boardgamegeek.com/boardgame/{}".format(self.BGGid)
        else:
            return None

    @property
    def last_session(self):
        return Session.objects.filter(game=self).order_by('-date_time').first()

    @property_method
    def last_performances(self, leagues=[], players=[], asat=None) -> object:
        '''
        Returns the last performances at this game (optionally as at a given date time) for
        a player or all players in specified leagues or all players in all leagues (if no
        leagues specified).

        Returns a Performance queryset.

        :param leagues:The league or leagues to consider when finding the last_performances. All leagues considered if none specified.
        :param player: The player or players to consider when finding the last_performances. All players considered if none specified.
        :param asat: Optionally, the last performance as at this date/time
        '''
        pfilter = Q(session__game=self)
        if leagues:
            pfilter &= Q(player__leagues__in=leagues)
        if players:
            pfilter &= Q(player__in=players)
        if not asat is None:
            pfilter &= Q(session__date_time__lte=asat)

        # Aggregate for max date_time for a given player. That is we want one Performance
        # per player, the one with the greatest date_time (that is before asat if specified)
        #
        # This seems to work, but I cannot find solid documentation on this kind of behaviour.
        #
        # it uses a subquery that references outer query.
        pfilter &= Q(session__pk=Subquery(
                        (Performance.objects
                            .filter(Q(player=OuterRef('player')) & pfilter)
                            .values('session__pk')
                            .order_by('-session__date_time')[:1]
                        ), output_field=models.PositiveBigIntegerField()))

        Ps = Performance.objects.filter(pfilter).order_by('-trueskill_eta_after')

        if settings.DEBUG:
            log.debug(f"Fetching latest performances for game '{self.name}' as at {asat} for leagues ({leagues}) and players ({players})")
            log.debug(f"SQL: {get_SQL(Ps)}")

        return Ps

    @property_method
    def session_list(self, leagues=[], asat=None) -> list:
        '''
        Returns a list of sessions that played this game. Useful for counting or traversing.

        Such a list is returned for the specified league or leagues or for all leagues if
        none are specified.

        Optionally can provide the list of sessions played as at a given date time.

        :param leagues: Returns sessions played considering the specified league or leagues or all leagues if none is specified.
        :param asat: Optionally returns the sessions played as at a given date
        '''
        # If a single league was provided make a list with one entry.
        if not isinstance(leagues, list):
            if leagues:
                leagues = [leagues]
            else:
                leagues = []

        # We can accept leagues as League instances or PKs but want a PK list for the queries.
        for l in range(0, len(leagues)):
            if isinstance(leagues[l], League):
                leagues[l] = leagues[l].pk
            elif not ((isinstance(leagues[l], str) and leagues[l].isdigit()) or isinstance(leagues[l], int)):
                raise ValueError(f"Unexpected league: {leagues[l]}.")

        if asat is None:
            if leagues:
                return Session.objects.filter(game=self, league__in=leagues)
            else:
                return Session.objects.filter(game=self)
        else:
            if leagues:
                return Session.objects.filter(game=self, league__in=leagues, date_time__lte=asat)
            else:
                return Session.objects.filter(game=self, date_time__lte=asat)

    @property_method
    def play_counts(self, leagues=[], asat=None) -> dict:
        '''
        Returns the number of plays this game has experienced, as a dictionary containing:
            total:    is the sum of all the individual player counts (so a count of total play experiences)
            max:      is the largest play count of any player
            average:  is the average play count of all players who've played at least once
            players:  is a count of players who played this game at least once
            sessions: is a count of the number of sessions this game has been played

        leagues can be a single league (a pk) or a list of leagues (pks).
        We always return the playcount across all the listed leagues.

        If no leagues are specified returns the play_counts for all leagues.

        Optionally can provide the count of plays as at a given date time as well.

        :param leagues: Returns playcounts considering the specified league or leagues or all leagues if none is specified.
        :param asat: Optionally returns the play counts as at a given date
        '''
        # If a single league was provided make a list with one entry.
        if not isinstance(leagues, list):
            if leagues:
                leagues = [leagues]
            else:
                leagues = []

        # We can accept leagues as League instances or PKs but want a PK list for the queries.
        for l in range(0, len(leagues)):
            if isinstance(leagues[l], League):
                leagues[l] = leagues[l].pk
            elif not ((isinstance(leagues[l], str) and leagues[l].isdigit()) or isinstance(leagues[l], int)):
                raise ValueError(f"Unexpected league: {leagues[l]}.")

        if asat is None:
            if leagues:
                ratings = Rating.objects.filter(game=self, player__leagues__in=leagues)
            else:
                ratings = Rating.objects.filter(game=self)

            pc = ratings.aggregate(total=Sum('plays'), max=Max('plays'), average=Avg('plays'), players=Count('plays'))
            for key in pc:
                if pc[key] is None:
                    pc[key] = 0

            pc['sessions'] = self.session_list(leagues).count()
        else:
            # Can't use the Ratings model as that stores current ratings (and play counts). Instead use the Performance
            # model which records ratings (and play counts) after every game session and the sessions have a date/time
            # so the information can be extracted therefrom.
            if leagues:
                performances = self.last_performances(leagues=leagues, asat=asat)
            else:
                performances = self.last_performances(asat=asat)

            # The play_number of the last performance is the play count at that time.
            pc = performances.aggregate(total=Sum('play_number'), max=Max('play_number'), average=Avg('play_number'), players=Count('play_number'))
            for key in pc:
                if pc[key] is None:
                    pc[key] = 0

            pc['sessions'] = self.session_list(leagues, asat=asat).count()

        return pc

    @property_method
    def leaderboard(self, leagues=[], asat=None, names="nick", style=LB_PLAYER_LIST_STYLE.simple, data=None) -> tuple:
        '''
        Return a a player list.

        The structure is described by LB_STRUCTURE.player_list

        This is an ordered tuple of tuples (one per player) that represents the leaderboard for
        specified leagues, or for all leagues if None is specified. As at a given date/time if
        such is specified, else, as at now (latest or current, leaderboard) source from the current
        database or the list provided in the data argument.

        :param leagues:   Show only players in any of these leagues if specified, else in any league (a single league or a list of leagues)
        :param asat:      Show the leaderboard as it was at this time rather than now, if specified
        :param names:     Specifies how names should be rendered in the leaderboard, one of the Player.name() options.
        :param style      The style of leaderboard to return, a LB_PLAYER_LIST_STYLE value
                          LB_PLAYER_LIST_STYLE.rich is special in that it will ignore league filtering and name formatting
                          providing rich data sufficent for the recipient to do that (choose what leagues to present and
                          how to present names.
        :param data:      Optionally this can provide a leaderboard in the style LEADERBOARD.data to use as source rather
                          than the database as a source of data! This is for restyling leaderboard data that has been saved
                          in the data style.
        '''
        # If a single league was provided make a list with one entry.
        if not isinstance(leagues, list):
            if leagues:
                leagues = [leagues]
            else:
                leagues = []

        if settings.DEBUG:
            log.debug(f"\t\tBuilding leaderboard for {self.name} as at {asat}.")

        # Assure itegrity of arguments
        if asat and data:
            raise ValueError(f"Game.leaderboards: Expected either asat or data and not both. I got {asat=} and {data=}.")

        if style == LB_PLAYER_LIST_STYLE.rich:
            # The rich syle contains extra info which allows the recipient to choose the name format (all name styles are included)
            if names != "nick":  # The default value
                raise ValueError(f"Game.leaderboards requested in rich style. Expected no names submitted but got: {names}")
            # The rich syle contains extra info which allows the recipient to filter on league (each player has their leagues identified in the board)
            if leagues:
                raise ValueError(f"Game.leaderboards requested in rich style. Expected no leagues submitted but got: {leagues}")

        # We can accept leagues as League instances or PKs but want a PK list for the queries.
        if leagues:
            for l in range(0, len(leagues)):
                if isinstance(leagues[l], League):
                    leagues[l] = leagues[l].pk
                elif not ((isinstance(leagues[l], str) and leagues[l].isdigit()) or isinstance(leagues[l], int)):
                    raise ValueError(f"Unexpected league: {leagues[l]}.")

            if settings.DEBUG:
                log.debug(f"\t\tValidated leagues")

        if data:
            if isinstance(data, str):
                ratings = json.loads(data)
            else:
                ratings = data
        elif asat:
            # Build leaderboard as at a given time as specified
            # Can't use the Ratings model as that stores current ratings. Instead use the Performance
            # model which records ratings after every game session and the sessions have a date/time
            # so the information can be extracted therefrom. These are returned in order -eta as well
            # so in the right order for a leaderboard (descending skill rating)
            ratings = self.last_performances(leagues=leagues, asat=asat)
        else:
            # We only want ratings from this game
            lb_filter = Q(game=self)

            # If leagues are specified we don't want to see people from other leagues
            # on this leaderboard, only players from the nominated leagues.
            if leagues:
                # TODO: FIXME: This is bold. player__leagues is a set, and leagues is a set
                # Does this yield the intersection or not? Requires a test!
                lb_filter = lb_filter & Q(player__leagues__in=leagues)

            ratings = Rating.objects.filter(lb_filter).order_by('-trueskill_eta').distinct()

        if settings.DEBUG:
            log.debug(f"\t\tBuilt ratings queryset.")

        # Now build a leaderboard from all the ratings for players (in this league) at this game.
        lb = []
        for r in ratings:
            # r may be a Rating object or a Performance object. They both have a player
            # but other metadata is specific. So we fetch them based on their accessibility
            if isinstance(r, Rating):
                player = r.player
                player_pk = player.pk
                trueskill_eta = r.trueskill_eta
                trueskill_mu = r.trueskill_mu
                trueskill_sigma = r.trueskill_sigma
                plays = r.plays
                victories = r.victories
                last_play = r.last_play_local
            elif isinstance(r, Performance):
                player = r.player
                player_pk = player.pk
                trueskill_eta = r.trueskill_eta_after
                trueskill_mu = r.trueskill_mu_after
                trueskill_sigma = r.trueskill_sigma_after
                plays = r.play_number
                victories = r.victory_count
                last_play = r.session.date_time_local
            elif isinstance(r, tuple) or isinstance(r, list):
                # Unpack the data tuple (as defined below where LB_PLAYER_LIST_STYLE.data tuples are created).
                player_pk, trueskill_eta, trueskill_mu, trueskill_sigma, plays, victories = r
                # TODO: consider the consequences of this choice of last_play in the rebuild log reporting.
                last_play = self.last_session.date_time_local
            else:
                raise ValueError(f"Progamming error in Game.leaderboard(). Unextected rating type: {type(r)}.")

            player_tuple = (player_pk, trueskill_eta, trueskill_mu, trueskill_sigma, plays, victories, last_play)
            lb.append(player_tuple)

        if settings.DEBUG:
            log.debug(f"\t\tBuilt leaderboard.")

        return None if len(lb) == 0 else styled_player_list(lb, style=style, names=names)

    @property_method
    def wrapped_leaderboard(self, leaderboard=None, snap=False, hide_baseline=False, leagues=[], asat=None, names="nick", style=LB_PLAYER_LIST_STYLE.simple, data=None) -> tuple:
        '''
        Returns a leaderboard (either a single board or a list of session snapshots) wrapped
        in a game propery header.

        The structure is decribed as
            LB_STRUCTURE.game_wrapped_player_list or
            LB_STRUCTURE.game_wrapped_session_wrapped_player_list

            depending on the stucturer of the supplied leaderboard which is either a board or a list o snapshots with structure:

            LB_STRUCTURE.player_list

            or a list of snapshots with structure:

            LB_STRUCTURE.session_wrapped_player_list

        A central defintion of the first tier of a the AJAX leaderboard view,
        a game header, which wraps a data delivery, the data being a
        leaderboard or list of leaderboard snapshots as defined in:

            Game.leaderboard
            Session.leaderboard_snapshot

        A game wrapper contains:
            game.pk,
            game.BGGid
            game.name
            total number of plays
            total number sessions played
            A flag True if data is a list, false if it is only a single value. The value is either a player_list or session_wrapped_player_list.
            A flag True to hide display of a baseline snapshot
            data (a playerlist or a session snapshot - session wrapped player list)

        :param leaderboard:  a leaderboard or a list of session wrapped leaderboard (snaphots) for this game
        :param snap:         if leaderboard is a list of snapshots, true, if leaderboard is a single leaderboard, false
        :param hide_baseline:if snap is True, then if the last snapshot is a baseline that should be hidden this is true, else False
        :param leagues:      self.leaderboard argument passed through
        :param asat:         self.leaderboard argument passed through
        :param names:        self.leaderboard argument passed through
        :param style:        self.leaderboard argument passed through
        :param data:         self.leaderboard argument passed through
        '''
        if leaderboard is None:
            leaderboard = self.leaderboard(leagues, asat, names, style, data)
            snap = False

        # Permit submission of an empty tuple () to return an empty tuple.
        if leaderboard:
            counts = self.play_counts()

            # TODO: Respect styles. IMportantly .data shoudl be minimlist reoctructable.
            # none might mean no wrapper
            # data drops the BGGid and name
            # rating and ratings map to simple
            # simple and rich are as currently implemented.
            return (self.pk, self.BGGid, self.name, counts['total'], counts['sessions'], snap, hide_baseline, leaderboard)
        else:
            return ()

    def rating(self, player, asat=None):
        '''
        Returns the Trueskill rating for this player at the specified game
        '''

        if asat is None:
            try:
                r = Rating.objects.get(player=player, game=self)
            except ObjectDoesNotExist:
                r = Rating.create(player=player, game=self)
            except MultipleObjectsReturned:
                raise ValueError("Database error: more than one rating for {} at {}".format(player.name_nickname, self.name))
            return r
        else:
            # Use the Performance model (and time stamped associated sessions) to construct
            # a rating object as at a specific date/time
            # TODO: Implement
            pass

    def future_sessions(self, asat, players=None) -> list:
        '''
        Returns a list of sessions ordered by date_time that are in future from
        the perspective of asat for the players provided, or for everyone if None.

        This is needed for rebuilding ratings when a historic game session
        detail changes.

        Returns a list not a QuerySet because there is a tree of future influence
        that a QuerySet cannot represent. The basic tree begins with all sessions
        in the future that one of this session's players particpated in. Each of
        those sessions though can rope in new players who add branches to the tree.

        :param asat:       a datetime from which persepective the "future" is.
        :param players:    a QuerySet of Players or a list of Players.
        '''
        # We want session in the future only of course
        dfilter = Q(date_time__gt=asat)

        # We want only sessions for this game
        gfilter = Q(game=self)

        # For each player we find all future sessions playing this game
        pfilter = Q(performances__player__in=players) if players else Q()

        # Combine the filters
        filters = dfilter & gfilter & pfilter

        future_sessions = Session.objects.filter(filters).distinct().order_by('date_time')

        sessions_so_far = list(future_sessions)

        # If no players were provided we already have ALL the future sessions of this game
        # If we specified some players, then we only have those that involved those players
        # We need recursively to examine that list because any player not in our list who
        # appears in a session with one these players has a whole future tree of influence
        # themselves.
        if players and future_sessions.count() > 0:
            # The new future sessions may involve new players which
            # requires that we scan them for new future sessions too
            for session in future_sessions:
                # session._get_future_sessions looks for new sessions (excluding sessions_so_far)
                # If it find any it adds them to sessions_so far and returns the complete list.
                new_sessions_so_far = session._get_future_sessions(sessions_so_far)

                # The returned list is bigger if any were added, else same size.
                # If it's bigger, sort it bu data_time and use that for the time
                # round the loop (of future_sessions)
                if len(new_sessions_so_far) > len(sessions_so_far):
                    sessions_so_far = sorted(new_sessions_so_far, key=lambda s: s.date_time)

        # After examining all future sessions, sessions_so_far is the complete list
        return sessions_so_far

    selector_field = "name"

    @classmethod
    def selector_queryset(cls, query="", session={}, all=False):
        '''
        Provides a queryset for ModelChoiceFields (select widgets) that ask for it.
        :param cls: Our class (so we can build a queryset on it to return)
        :param query: A simple string being a query that is submitted (typically typed into a django-autcomplete-light ModelSelect2 or ModelSelect2Multiple widget)
        :param session: The request session (if there's a filter recorded there we honor it)
        :param all: Requests to ignore any default league filtering
        '''
        qs = cls.objects.all()

        if not all:
            league = session.get('filter', {}).get('league', None)
            if league:
                qs = qs.filter(leagues=league)

        if query:
            # TODO: Should really respect s['filter_priorities'] as the list view does.
            qs = qs.filter(**{f'{cls.selector_field}__istartswith': query})

        qs = qs.annotate(play_count=Count('sessions')).order_by("-play_count")

        return qs

    add_related = None

    def __unicode__(self): return getattr(self, self.selector_field)

    def __str__(self): return self.__unicode__()

    def __verbose_str__(self):
        return f'{self.name} (plays {self.min_players}-{self.max_players})'

    def __rich_str__(self, link=None):
        name = field_render(self.name, link_target_url(self, link))
        pmin = self.min_players
        pmax = self.max_players
        beta = self.trueskill_beta
        tau = self.trueskill_tau * 100
        p = int(self.trueskill_p * 100)
        return f'{name} (plays {pmin}-{pmax}), Skill factor: {beta:0.2f}, Draw probability: {p:d}%, Skill dynamics factor: {tau:0.2f}'

    def __detail_str__(self, link=None):
        detail = self.__rich_str__(link)

        plays = self.league_plays

        detail += "<BR>Play counts:<UL>"
        for league in plays:
            if league == ALL_LEAGUES:
                # Don't show the global count if there's only one league!
                if len(plays) > 2:
                    league_str = "across all leagues."
                else:
                    league_str = ""
            else:
                league_str = f"in League: {field_render(league, link)}"

            if league_str:
                detail += f"<LI>{plays[league]['sessions']} plays and {plays[league]['players']} players {league_str}</LI>"
        detail += "</UL>"

        return detail

    def check_integrity(self, passthru=True):
        '''
        Perform integrity check on this Game record
        '''
        L = AssertLog(passthru)

        pfx = f"Game Integrity error (id: {self.id}):"

        # Check that it has all leagues registered that played it
        leagues_played = set(Session.objects.filter(game=self).values_list('league'))
        leagues_registered = set(self.leagues.all().values_list('pk'))
        L.Assert(leagues_played == leagues_registered, f"{pfx} Played in leagues: {leagues_played}, registered with leagues: {leagues_registered}")

        # Check that only allowed play modes were played
        if not self.individual_play:
            individual_plays = Session.objects.filter(game=self, team_play=False).values_list('pk')
            L.Assert(not individual_plays, f"{pfx} Only team play allowed but these sessions recorded with individual play: {individual_plays}")

        if not self.team_play:
            team_plays = Session.objects.filter(game=self, team_play=True).values_list('pk')
            L.Assert(not team_plays, f"{pfx} Only individual play allowed but these sessions recorded with team play: {team_plays}")

        # Check player counts within limits
        sessions = Session.objects.annotate(player_count=Count('performances')).filter(game=self)
        too_few = sessions.filter(player_count__lt=self.min_players).values_list('pk')
        L.Assert(not too_few, f"{pfx} Too few players in these sessions: {too_few}")

        too_many = sessions.filter(player_count__gt=self.max_players).values_list('pk')
        L.Assert(not too_many, f"{pfx} Too many players in these sessions: {too_many}")

        # Check team sizes within limits
        if self.team_play:
            teams = Team.objects.annotate(player_count=Count('players')).filter(ranks__session__game=self)

            too_few = teams.filter(player_count__lt=self.min_players_per_team).values_list('pk')
            L.Assert(not too_few, f"{pfx} Too few players in these teams: {too_few}")

            too_many = teams.filter(player_count__gt=self.max_players_per_team).values_list('pk')
            L.Assert(not too_many, f"{pfx} Too many players in these teams: {too_many}")

        # Check all trueskill parameters are consistent with recorded session performances
        performances = (Performance.objects.filter(session__game=self).
            annotate(diff_beta=Func(F('trueskill_beta') - self.trueskill_beta, function='ABS')).
            annotate(diff_tau=Func(F('trueskill_tau') - self.trueskill_tau, function='ABS')).
            annotate(diff_p=Func(F('trueskill_p') - self.trueskill_p, function='ABS')))

        wrong_betas = performances.filter(diff_beta__gt=FLOAT_TOLERANCE).values_list('pk')
        L.Assert(not wrong_betas, f"{pfx} Incorrect Betas recorded on Performances: {wrong_betas}")

        wrong_taus = performances.filter(diff_tau__gt=FLOAT_TOLERANCE).values_list('pk')
        L.Assert(not wrong_taus, f"{pfx} Incorrect Taus recorded on Performances: {wrong_taus}")

        wrong_ps = performances.filter(diff_p__gt=FLOAT_TOLERANCE).values_list('pk')
        L.Assert(not wrong_ps, f"{pfx} Incorrect ps recorded on Performances: {wrong_ps}")

        return L.assertion_failures

    class Meta(AdminModel.Meta):
        verbose_name = "Game"
        verbose_name_plural = "Games"
        ordering = ['name']
        constraints = [ models.UniqueConstraint(fields=['name'], name='unique_game_name') ]


class Location(AdminModel):
    '''
    A location that a game session can take place at.
    '''
    name = models.CharField('Name of the Location', max_length=MAX_NAME_LENGTH)
    timezone = TimeZoneField('Timezone of the Location', default=settings.TIME_ZONE)
    location = LocationField('Geolocation of the Location', blank=True)
    leagues = models.ManyToManyField(League, verbose_name='Leagues using the Location', blank=True, related_name='Locations_used', through=League.locations.through)

    @property
    def link_internal(self) -> str:
        return reverse('view', kwargs={"model":self._meta.model.__name__, "pk": self.pk})

    selector_field = "name"

    @classmethod
    def selector_queryset(cls, query="", session={}, all=False):  # @ReservedAssignment
        '''
        Provides a queryset for ModelChoiceFields (select widgets) that ask for it.
        :param cls: Our class (so we can build a queryset on it to return)
        :param query: A simple string being a query that is submitted (typically typed into a django-autcomplete-light ModelSelect2 or ModelSelect2Multiple widget)
        :param session: The request session (if there's a filter recorded there we honor it)
        :param all: Requests to ignore any default league filtering
        '''
        qs = cls.objects.all()

        if not all:
            league = session.get('filter', {}).get('league', None)
            if league:
                # TODO: Should really respect s['filter_priorities'] as the list view does.
                qs = qs.filter(leagues=league)

        if query:
            qs = qs.filter(**{f'{cls.selector_field}__istartswith': query})

        return qs

    add_related = None

    def __unicode__(self): return getattr(self, self.selector_field)

    def __str__(self): return self.__unicode__()

    def __verbose_str__(self):
        return u"{} (used by: {})".format(self.__str__(), ", ".join(list(self.leagues.all().values_list('name', flat=True))))

    def __rich_str__(self, link=None):
        leagues = list(self.leagues.all())
        leagues = list(map(lambda l: field_render(l, link), leagues))
        return u"{} (used by: {})".format(field_render(self, link), ", ".join(leagues))

    class Meta(AdminModel.Meta):
        verbose_name = "Location"
        verbose_name_plural = "Locations"
        ordering = ['name']


class Event(AdminModel):
    '''
    A model for defining gaming events. The idea being that we can show all leaderboards
    relevant to a particular event (games and tourneys) and specify the time bracket and
    venue for the event so that the recorded game sessions belonging to they event can
    be inferred.

    Timezones are ignored as they are inferred from the Location which has a timezone.
    '''
    location = models.ForeignKey(Location, verbose_name='Event location', null=True, blank=-True, on_delete=models.SET_NULL)  # If the location is deleted keep the event.
    start = models.DateTimeField('Time', default=timezone.now)
    end = models.DateTimeField('Time', default=timezone.now)
    registrars = models.ManyToManyField('Player', verbose_name='Registrars', blank=True, related_name='registrar_at')

    class Meta(AdminModel.Meta):
        verbose_name = "Event"
        verbose_name_plural = "Events"


class Session(TimeZoneMixIn, AdminModel):
    '''
    The record, with results (Ranks), of a particular Game being played competitively.
    '''
    game = models.ForeignKey(Game, verbose_name='Game', related_name='sessions', null=True, on_delete=models.SET_NULL)  # If the game is deleted keep the session.

    date_time = models.DateTimeField('Time', default=timezone.now)
    date_time_tz = TimeZoneField('Timezone', default=settings.TIME_ZONE, editable=False)

    league = models.ForeignKey(League, verbose_name='League', related_name='sessions', null=True, on_delete=models.SET_NULL)  # If the league is deleted keep the session
    location = models.ForeignKey(Location, verbose_name='Location', related_name='sessions', null=True, on_delete=models.SET_NULL)  # If the location is deleted keep the session

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

    @property
    def ranked_players(self) -> dict:
        '''
        Returns an OrderedDict (keyed on rank) of the players in the session.
        The value is either a player (for individual play sessions) or
            a list of players (in team play sessions)

        Note ties have the same rank, so the key has a .index appended,
        to form a unique key. Only the key digits up to the . represent
        the true rank, the full key permits sorting and unique storage
        in a dictionary.
        '''
        players = OrderedDict()
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
            # rank is the rank object, rank.rank is the integer rank (1, 2, 3).
            if self.team_play:
                if rank_counts[rank.rank] > 1:
                    pid = 1
                    for player in rank.players:
                        players["{}.{}.{}".format(rank.rank, rank_id[rank.rank], pid)] = player
                        pid += 1
                    rank_id[rank.rank] += 1
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
                if rank_counts[rank.rank] > 1:
                    players["{}.{}".format(rank.rank, rank_id[rank.rank])] = rank.player
                    rank_id[rank.rank] += 1
                else:
                    players["{}".format(rank.rank)] = rank.player
        return players

    @property
    def players(self) -> set:
        '''
        Returns an unordered set of the players in the session, with no guaranteed
        order. Useful for traversing a list of all players in a session
        irrespective of the structure of teams or otherwise.

        '''
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

        return self.game.wrapped_leaderboard(sw_boards, snap=True, hide_baseline=include_latest)

    @property
    def player_ranking_impact(self) -> dict:
        '''
        Returns a dict keyed on player (whose ratings were affected by by this rebuild) whose value is their rank change on the leaderboard.
        '''

        before = self.leaderboard_before(style=LB_PLAYER_LIST_STYLE.data, wrap=False)  # NOT session wrapped
        after = self.leaderboard_after(style=LB_PLAYER_LIST_STYLE.data, wrap=False)  # NOT session wrapped

        deltas = {}
        old = player_rankings(before, structure=LB_STRUCTURE.player_list)
        new = player_rankings(after, structure=LB_STRUCTURE.player_list)

        for p in old:
            if not new[p] == old[p]:
                delta = new[p] - old[p]
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
            P = ", ".join([p.name_nickname for p in self.players])
            # venue = f"- {field_render(self.location, link)}"
            return (f'{time_str(self.date_time)} - {field_render(self.league, link)} - '
                   +f'{field_render(self.game, link)} - {self.num_competitors} {self.str_competitors} ({P}) - {V} won')
        except:
            pass

    def __detail_str__(self, link=None):
        detail = time_str(self.date_time) + "<br>"
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


class Rank(AdminModel):
    '''
    The record, for a given Session of a Rank (i.e. 1st, 2nd, 3rd etc) for a specified Player or Team.

    Either a player or team is specified, neither or both is a data error.
    Which one, is specified in the Session model where a record is kept of whether this was a Team play session or not (i.e. Individual play)
    '''
    session = models.ForeignKey(Session, verbose_name='Session', related_name='ranks', on_delete=models.CASCADE)  # if the session is deleted, delete this rank
    rank = models.PositiveIntegerField('Rank')  # The rank (in this session) we are recording, as in 1st, 2nd, 3rd etc.
    score = models.IntegerField('Score', default=None, null=True, blank=True)  # What this team scored if the game has team scores.

    # One or the other of these has a value the other should be null (enforce in integrity checks)
    # We coudlof course opt to use a single GenericForeignKey here:
    #    https://docs.djangoproject.com/en/1.10/ref/contrib/contenttypes/#generic-relations
    #    but there are some complexites they introduce that are rather unnatracive as well
    player = models.ForeignKey(Player, verbose_name='Player', blank=True, null=True, related_name='ranks', on_delete=models.SET_NULL)  # If the player is deleted keep this rank
    team = models.ForeignKey(Team, verbose_name='Team', blank=True, null=True, related_name='ranks', on_delete=models.SET_NULL)  # if the team is deleted keep this rank

    add_related = ["player", "team"]  # When adding a Rank, add the related Players or Teams (if needed, or not if already in database)

    @property
    def performance(self):
        '''
        Returns a TrueSkill Performance for this ranking player or team. Uses very TrueSkill specific theory to provide
        a tuple of mean and standard deviation (mu, sigma) that describes TrueSkill Performance prediction.
        '''
        g = self.session.game
        ts = TrueSkillHelpers(tau=g.trueskill_tau, beta=g.trueskill_beta, p=g.trueskill_p)

        return ts.Rank_performance(self, after=False)

    @property
    def performance_after(self):
        '''
        Returns a TrueSkill Performance for this ranking player or team using the ratings the received after this session update.
        Uses very TrueSkil specific theory to provide a tuple of mean and standard deviation (mu, sigma) that describes TrueSkill
        Performance prediction.
        '''
        g = self.session.game
        ts = TrueSkillHelpers(tau=g.trueskill_tau, beta=g.trueskill_beta, p=g.trueskill_p)

        return ts.Rank_performance(self, after=True)

    @property
    def ranker(self) -> object:
        '''
        Returns either a Player or Team, as appropriate for the ranker,
        that is the player or team ranking here
        '''
        if self.session.team_play:
            return self.team
        else:
            return self.player

    @property
    def players(self) -> set:
        '''
        The list of players associated with this rank object (not explicitly at this rank
        as two Rank objects in one session may have the same rank, i.e. a draw may be recorded)

        Players in teams are listed individually.

        Returns a list of one one or more players.
        '''
        session = Session.objects.get(id=self.session.id)
        if session.team_play:
            if self.team is None:
                raise ValueError("Rank '{}' is associated with a team play session but has no team.".format(self.id))
            else:
                # TODO: Test that this returns a clean list and not a QuerySet
                players = set(self.team.players.all())
        else:
            if self.player is None:
                raise ValueError("Rank '{0}' is associated with an individual play session but has no player.".format(self.id))
            else:
                players = {self.player}
        return players

    @property
    def is_part_of_draw(self) -> bool:
        '''
        Returns True or False, indicating whether or not more than one rank object on this session has the same rank
        (i.e. if this rank object is one part of a recorded draw).
        '''
        ranks = Rank.objects.filter(session=self.session, rank=self.rank)
        return len(ranks) > 1

    @property
    def is_victory(self) -> bool:
        '''
        True if this rank records a victory, False if not.
        '''
        return self.rank == 1

    @property
    def is_team_rank(self):
        return self.session.team_play

    @property
    def link_internal(self) -> str:
        return reverse('view', kwargs={"model":self._meta.model.__name__, "pk": self.pk})

    # @property_method
    def check_integrity(self, passthru=True):
        '''
        Perform basic integrity checks on this Rank object.
        '''
        L = AssertLog(passthru)

        pfx = f"Rank Integrity error (id: {self.id}):"

        # Check that one of self.player and self.team has a valid value and the other is None
        L.Assert(not (self.team is None and self.player is None), f"{pfx} No team or player specified!")
        L.Assert(not (not self.team is None and not self.player is None), f"{pfx} Both team and player are specified!")

        if self.team is None:
            L.Assert(not self.session.team_play, f"{pfx} Rank specifies a player while session (ID: {self.session.pk}) specifies team play")
        elif self.player is None:
            L.Assert(self.session.team_play, f"{pfx} Rank specifies a team while session (ID: {self.session.pk}) does not specify team play")

        return L.assertion_failures

    def __unicode__(self):
        return "{}".format(self.rank)

    def __str__(self): return self.__unicode__()

    def __verbose_str__(self):
        if self.session is None:  # Don't crash of the rank is orphaned!
            game = "<no game>"
            ranker = self.player
        else:
            game = self.session.game
            ranker = self.team if self.session.team_play else self.player
        return  u'{} - {} - {}'.format(game, self.rank, ranker)

    def __rich_str__(self, link=None):
        if self.session is None:  # Don't crash of the rank is orphaned!
            game = "<no game>"
            team_play = False
            ranker = field_render(self.player, link)
        else:
            game = field_render(self.session.game, link)
            team_play = self.session.team_play
            ranker = field_render(self.team, link) if team_play else field_render(self.player, link)
        return  u'{} - {} - {}'.format(game, field_render(self.rank, link_target_url(self, link)), ranker)

    def __detail_str__(self, link=None):
        if self.session is None:  # Don't crash of the rank is orphaned!
            game = "<no game>"
            team_play = False
            ranker = field_render(self.player, link)
            mode = "individual"
        else:
            game = field_render(self.session.game, link)
            team_play = self.session.team_play
            ranker = field_render(self.team, link) if team_play else field_render(self.player, link)
            mode = "team" if team_play else "individual"
        return  u'{} - {} - {} ({} play)'.format(game, field_render(self.rank, link_target_url(self, link)), ranker, mode)

    def clean(self):
        # Require that one of self.team and self.player is None
        if self.team is None and self.player is None:
            # FIXME: For now let clean pass with no team if teamplay is selected.
            # This is because the team is not created until post processing (after the clean and save)
            # at present. This should be rethought and perhaps done in pre-processing so as
            # to present teams for the validation (and clean).
            # But because we don't know if it's a team_play session or not, that is,
            # clean is happening before the link to the session is made (self.session_id = None
            # we have to pass this condition always for now.
            # raise ValidationError("No team or player specified in rank {}".format(self.pk))
            pass
        # When editing Rank objects and changing the team_play setting in the associated setting,
        # It can easily be that a team is added and a player remains. Clean up any duplicity on
        # submission.
        if not self.team is None and not self.player is None:
            if not hasattr(self.session, "team_play"):
                raise ValidationError("Both team and player specified in rank {} but it has no associated session so we can't clean it.".format(self.pk))

            if self.session.team_play:
                self.player = None
            else:
                self.team = None

    class Meta(AdminModel.Meta):
        verbose_name = "Rank"
        verbose_name_plural = "Ranks"
        ordering = ['rank']


class Performance(AdminModel):
    '''
    Each player in each session has a Performance associated with them.

    The only input here is the partial play weighting, which models just that, early departure from the game session before the game is complete.
    But can be used to arbitrarily weight contributions of players to teams as well if desired.

    This model also contains for each session a record of the calculated Trueskill performance of that player, namely trueskill values before
    and after the play (for data redundancy as the after values of one session are the before values of the next for a given player, which can
    also be asserted for data integrity).
    '''
    TS = TrueskillSettings()

    session = models.ForeignKey(Session, verbose_name='Session', related_name='performances', on_delete=models.CASCADE)  # If the session is deleted, dlete this performance
    player = models.ForeignKey(Player, verbose_name='Player', related_name='performances', null=True, on_delete=models.SET_NULL)  # if the player is deleted keep this performance

    partial_play_weighting = models.FloatField('Partial Play Weighting (ω)', default=1)

    score = models.IntegerField('Score', default=None, null=True, blank=True)  # What this player scored if the game has scores.

    play_number = models.PositiveIntegerField('The number of this play (for this player at this game)', default=1, editable=False)
    victory_count = models.PositiveIntegerField('The count of victories after this session (for this player at this game)', default=0, editable=False)

    # Although Eta (η) is a simple function of Mu (µ) and Sigma (σ), we store it alongside Mu and Sigma because it is also a function of global settings µ0 and σ0.
    # To protect ourselves against changes to those global settings, or simply to detect them if it should happen, we capture their value at time of rating update in the Eta.
    # The before values are copied from the Rating for that Player/Game combo and the after values are written back to that Rating.
    trueskill_mu_before = models.FloatField('Trueskill Mean (µ) before the session.', default=TS.mu0, editable=False)
    trueskill_sigma_before = models.FloatField('Trueskill Standard Deviation (σ) before the session.', default=TS.sigma0, editable=False)
    trueskill_eta_before = models.FloatField('Trueskill Rating (η) before the session.', default=0, editable=False)

    trueskill_mu_after = models.FloatField('Trueskill Mean (µ) after the session.', default=TS.mu0, editable=False)
    trueskill_sigma_after = models.FloatField('Trueskill Standard Deviation (σ) after the session.', default=TS.sigma0, editable=False)
    trueskill_eta_after = models.FloatField('Trueskill Rating (η) after the session.', default=0, editable=False)

    # Record the global TrueskillSettings mu0, sigma0 and delta with each performance
    # This will allow us to reset ratings to the state they were at after this performance
    # It is an integrity measure as well against changes in these settings while a leaderboard
    # is running, which has significant consequences (suggesting a rebuild of all ratings is in
    # order)
    trueskill_mu0 = models.FloatField('Trueskill Initial Mean (µ)', default=trueskill.MU, editable=False)
    trueskill_sigma0 = models.FloatField('Trueskill Initial Standard Deviation (σ)', default=trueskill.SIGMA, editable=False)
    trueskill_delta = models.FloatField('TrueSkill Delta (δ)', default=trueskill.DELTA, editable=False)

    # Record the game specific Trueskill settings beta, tau and p with each performance.
    # Again for a given game these must be consistent among all ratings and the history of each rating.
    # Any change while managing leaderboards should trigger an update request for ratings relating to this game.
    trueskill_beta = models.FloatField('TrueSkill Skill Factor (ß)', default=trueskill.BETA, editable=False)
    trueskill_tau = models.FloatField('TrueSkill Dynamics Factor (τ)', default=trueskill.TAU, editable=False)
    trueskill_p = models.FloatField('TrueSkill Draw Probability (p)', default=trueskill.DRAW_PROBABILITY, editable=False)

    @property
    def game(self) -> Game:
        '''
        The game this performance relates to
        '''
        return self.session.game

    @property
    def date_time(self) -> datetime:
        '''
        The game this performance relates to
        '''
        return self.session.date_time

    @property
    def rank(self) -> Rank:
        '''
        The rank of this player in this session. Most certainly a component of a player's
        performance, but not stored in the Performance model because it is associated either
        with a player or whole team depending on the play mode (Individual or Team). So this
        property fetches the rank from the Rank model where it's stored.
        '''
        sfilter = Q(session=self.session)
        ipfilter = Q(player=self.player)
        tpfilter = Q(team__players=self.player)
        rfilter = sfilter & (ipfilter | tpfilter)
        return Rank.objects.filter(rfilter).first()

    @property
    def is_victory(self) -> bool:
        '''
        True if this performance records a victory for the player, False if not.
        '''
        return self.rank == 1

    @property
    def rating(self) -> Rating:
        '''
        Returns the rating object associated with this performance. That is for the same player/game combo.
        '''
        try:
            r = Rating.objects.get(player=self.player, game=self.session.game)
        except ObjectDoesNotExist:
            r = Rating.create(player=self.player, game=self.session.game)
        except MultipleObjectsReturned:
            raise ValueError("Database error: more than one rating for {} at {}".format(self.player.name_nickname, self.session.game.name))
        return r

    @property
    def previous_play(self) -> 'Performance':
        '''
        Returns the previous performance object that this player played this game in.
        '''
        return self.session.previous_performance(self.player)

    @property
    def previous_win(self) -> 'Performance':
        '''
        Returns the previous performance object that this player played this game in and one in
        '''
        return self.session.previous_victory(self.player)

    @property
    def link_internal(self) -> str:
        return reverse('view', kwargs={"model":self._meta.model.__name__, "pk": self.pk})

    def initialise(self, save=False):
        '''
        Initialises the performance object.

        With the session and player already known,
        finds the previous session that this player played this game in
        and copies the after ratings of the previous performance (for this
        player at this session's game) to the before ratings of this
        performance object for that player.
        '''

        previous = self.session.previous_performance(self.player)

        if previous is None:
            TSS = TrueskillSettings()
            self.play_number = 1
            self.victory_count = 1 if self.session.rank(self.player).rank == 1 else 0
            self.trueskill_mu_before = TSS.mu0
            self.trueskill_sigma_before = TSS.sigma0
            self.trueskill_eta_before = 0

        else:
            self.play_number = previous.play_number + 1
            self.victory_count = previous.victory_count + 1 if self.session.rank(self.player).rank == 1 else previous.victory_count
            self.trueskill_mu_before = previous.trueskill_mu_after
            self.trueskill_sigma_before = previous.trueskill_sigma_after
            self.trueskill_eta_before = previous.trueskill_eta_after

        # Capture the Trueskill settings that are in place now too.
        TS = TrueskillSettings()
        self.trueskill_mu0 = TS.mu0
        self.trueskill_sigma0 = TS.sigma0
        self.trueskill_delta = TS.delta
        self.trueskill_beta = self.session.game.trueskill_beta
        self.trueskill_tau = self.session.game.trueskill_tau
        self.trueskill_p = self.session.game.trueskill_p

        if save:
            self.save()

    def check_integrity(self, passthru=True):
        '''
        Perform basic integrity checks on this Performance object.
        '''
        L = AssertLog(passthru)

        pfx = f"Performance Integrity error (id: {self.id}):"

        # Check that the before values match the after values of the previous play
        performance = self
        previous = self.previous_play

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

        # Check that the Trueskill settings are consistent with previous play too
        if previous is None:
            TS = TrueskillSettings()

            L.Assert(isclose(performance.trueskill_mu0, TS.mu0, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance µ0 mismatch. At {performance.session.date_time} is {performance.trueskill_mu0} and previous at Never is {TS.mu0}")
            L.Assert(isclose(performance.trueskill_sigma0, TS.sigma0, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance σ0 mismatch. At {performance.session.date_time} is {performance.trueskill_sigma0} and on previous at Never is {TS.sigma0}")
            L.Assert(isclose(performance.trueskill_delta, TS.delta, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance δ mismatch. At {performance.session.date_time} is {performance.trueskill_delta} and previous at Never is {TS.delta}")
        else:
            L.Assert(isclose(performance.trueskill_mu0, previous.trueskill_mu0, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance µ0 mismatch. At {performance.session.date_time} is {performance.trueskill_mu0} and previous at {previous.session.date_time} is {previous.trueskill_mu0}")
            L.Assert(isclose(performance.trueskill_sigma0, previous.trueskill_sigma0, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance σ0 mismatch. At {performance.session.date_time} is {performance.trueskill_sigma0} and previous at {previous.session.date_time} is {previous.trueskill_sigma0}")
            L.Assert(isclose(performance.trueskill_delta, previous.trueskill_delta, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance δ mismatch. At {performance.session.date_time} is {performance.trueskill_delta} and previous at {previous.session.date_time} is {previous.trueskill_delta}")
            L.Assert(isclose(performance.trueskill_beta, previous.trueskill_beta, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance ß mismatch. At {performance.session.date_time} is {performance.trueskill_beta} and previous at {previous.session.date_time} is {previous.trueskill_beta}")
            L.Assert(isclose(performance.trueskill_tau, previous.trueskill_tau, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance τ mismatch. At {performance.session.date_time} is {performance.trueskill_tau} and previous at {previous.session.date_time} is {previous.trueskill_tau}")
            L.Assert(isclose(performance.trueskill_p, previous.trueskill_p, abs_tol=FLOAT_TOLERANCE), f"{pfx} Performance p mismatch. At {performance.session.date_time} is {performance.trueskill_p} and previous at {previous.session.date_time} is {previous.trueskill_p}")

        # Check that there is an associate Rank
        L.Assert(not self.rank is None, f"{pfx} Has no associated rank!")

        # if self.player.pk == 1 and self.session.game.pk == 29 and self.session.date_time.year == 2021:
            # breakpoint()

        # Check that play number and victory count reflect earlier records
        expected_play_number = self.session.previous_sessions(self.player).count()  # Includes the current sessions
        expected_victory_count = self.session.previous_victories(self.player).count()  # Includes the current session if it's a victory

        L.Assert(self.play_number == expected_play_number, f"{pfx} Play number is wrong. Play number: {self.play_number}, Expected: {expected_play_number}.")
        L.Assert(self.victory_count == expected_victory_count, f"{pfx} Victory count is wrong. Victory count: {self.victory_count}, Expected: {expected_victory_count}.")

        return L.assertion_failures

    def clean(self):
        return  # Disable for now, enable only for testing

        # Find the previous performance for this player at this game and copy
        # the trueskill after values to the trueskill before values in this
        # performance or from initials if no previous.
        previous = self.previous_play

        if previous is None:
            TS = TrueskillSettings()

            self.trueskill_mu_before = TS.mu0
            self.trueskill_sigma_before = TS.sigma0
            self.trueskill_eta_before = 0
        else:
            self.trueskill_mu_before = previous.trueskill_mu_after
            self.trueskill_sigma_before = previous.trueskill_sigma_after
            self.trueskill_eta_before = previous.trueskill_eta_after

        # Catch the Trueskill settings in effect now.
        TS = TrueskillSettings()
        self.trueskill_mu0 = TS.mu0
        self.trueskill_sigma0 = TS.sigma0
        self.trueskill_beta = TS.beta
        self.trueskill_delta = TS.delta
        self.trueskill_tau = self.session.game.trueskill_tau
        self.trueskill_p = self.session.game.trueskill_p

        # If any of these settings have changed since the previous performance (for this player at this game)
        # Then we have an error that demands a rebuild of ratings for this game.
        if not previous is None:
            if self.trueskill_mu0 != previous.trueskill_mu0:
                raise ValidationError("Global Trueskill µ0 has changed (from {} to {}). Either reset the value of rebuild all ratings for game {} ({}) ".format(self.trueskill_mu0, previous.trueskill_mu0, self.session.game.pk, self.session.game))
            if self.trueskill_sigma0 != previous.trueskill_sigma0:
                raise ValidationError("Global Trueskill σ0 has changed (from {} to {}). Either reset the value of rebuild all ratings for game {} ({}) ".format(self.trueskill_sigma0, previous.trueskill_sigma0, self.session.game.pk, self.session.game))
            if self.trueskill_beta != previous.trueskill_beta:
                raise ValidationError("Global Trueskill ß has changed (from {} to {}). Either reset the value of rebuild all ratings for game {} ({}) ".format(self.trueskill_beta, previous.trueskill_beta, self.session.game.pk, self.session.game))
            if self.trueskill_delta != previous.trueskill_delta:
                raise ValidationError("Global Trueskill δ has changed (from {} to {}). Either reset the value of rebuild all ratings for game {} ({}) ".format(self.trueskill_delta, previous.trueskill_delta, self.session.game.pk, self.session.game))
            if self.trueskill_tau != previous.trueskill_tau:
                raise ValidationError("Game Trueskill τ has changed (from {} to {}). Either reset the value of rebuild all ratings for game {} ({}) ".format(self.trueskill_tau, previous.trueskill_tau, self.session.game.pk, self.session.game))
            if self.trueskill_p != previous.trueskill_p:
                raise ValidationError("Game Trueskill p has changed (from {} to {}). Either reset the value of rebuild all ratings for game {} ({}) ".format(self.trueskill_p, previous.trueskill_p, self.session.game.pk, self.session.game))

        # Update the play counters too. We know this form submisison means one more play but we don't necessariuly know if it's
        # a victury yet (as that is stored with an associated Rank which may or may not have been saved yet).
        self.play_number = previous.play_number + 1

        if self.session.rank(self.player):
            self.victory_count = previous.victory_count + 1 if self.session.rank(self.player).rank == 1 else previous.victory_count

        # Trueskill Impact is calculated at the session level not the individual performance level.
        # The trueskill after settings for the performance will be calculated there.
        pass

    add_related = None
    sort_by = ['session.date_time', 'rank.rank', 'player.name_nickname']  # Need player to sort ties and team members.

    # It is crucial that Performances for a session are ordered the same as Ranks when a rich from is constructed
    # Each row on a form in a standard session submission has a rank and a performance associated with it and the
    # player for each object must agree.
    @classmethod
    def form_order(cls, performances) -> QuerySet:
        '''
        Form field ordering support for Django Generic Form Extenions RelatedFormsets

        if this class method exists, DGFE will call it to order objects when building related forms.

        This returns the performances in order of the players ranking. Must return a QuerySet.

        :param performances:  A QuerySet
        '''
        # Annotate with ranking ('rank' is a method above and clashes, crashing the queryset evaluation)
        sfilter = Q(session=OuterRef('session'))
        ipfilter = Q(player=OuterRef('player'))
        tpfilter = Q(team__players=OuterRef('player'))
        rfilter = sfilter & (ipfilter | tpfilter)
        ranking = Subquery(Rank.objects.filter(rfilter).values('rank'))
        ranked_performances = performances.annotate(ranking=ranking)
        return ranked_performances.order_by('ranking')

    def __unicode__(self):
        return  u'{}'.format(self.player)

    def __str__(self): return self.__unicode__()

    def __verbose_str__(self):
        if self.session is None:  # Don't crash of the performance is orphaned!
            when = "<no time>"
            game = "<no game>"
        else:
            when = self.session.date_time
            game = self.session.game
        performer = self.player
        return  u'{} - {:%d, %b %Y} - {}'.format(game, when, performer)

    def __rich_str__(self, link=None):
        if self.session is None:  # Don't crash of the performance is orphaned!
            when = "<no time>"
            game = "<no game>"
        else:
            when = self.session.date_time
            game = field_render(self.session.game, link)
        performer = field_render(self.player, link)
        performance = "{:.0%} participation, play number {}, {:+.1f} teeth".format(self.partial_play_weighting, self.play_number, self.trueskill_eta_after - self.trueskill_eta_before)
        return  u'{} - {:%d, %b %Y} - {}: {}'.format(game, when, performer, field_render(performance, link_target_url(self, link)))

    def __detail_str__(self, link=None):
        if self.session is None:  # Don't crash of the performance is orphaned!
            when = "<no time>"
            game = "<no game>"
            players = "<no players>"
        else:
            when = self.session.date_time
            game = field_render(self.session.game, link)
            players = len(self.session.players)

        performer = field_render(self.player, link)

        detail = u'{} - {:%a, %-d %b %Y} - {}:<UL>'.format(game, when, performer)
        detail += "<LI>Players: {}</LI>".format(players)
        detail += "<LI>Play number: {}</LI>".format(self.play_number)
        detail += "<LI>Play Weighting: {:.0%}</LI>".format(self.partial_play_weighting)
        detail += "<LI>Trueskill Delta: {:+.1f} teeth</LI>".format(self.trueskill_eta_after - self.trueskill_eta_before)
        detail += "<LI>Victories: {}</LI>".format(self.victory_count)
        detail += "</UL>"
        return detail

    class Meta(AdminModel.Meta):
        verbose_name = "Performance"
        verbose_name_plural = "Performances"
        ordering = ['session', 'player']

#===============================================================================
# Administrative models
#===============================================================================


class ChangeLog(AdminModel):
    '''
    A model for storing a log of edits to recorded sessions. Any such edit has an immediate impact,
    on a leaderboard (i.e a leaderboard before it happened and a learboard after it happened. These
    are stored here in this model). A log can be written any time a session is added or edited, and the
    Admin fields on the entry record who did that, while the object stores leaderboard snaphots
    before and after (the session, not the edit).

    Such logs can be expired as well (as storing a full before and after leaderboard for ever session
    add or edit adds a lot of data, and has diminishing value when the entries are considered stable.
    This model is mainly for report impacts before a commit linking to a rebuild log if a rating rebuild
    was triggered.

    A session is defined by:
        a game
        a time
        a league
        a location (venue)
        a mode (team or individual depending on what the game supports)
        a list of ranks (one per ranker, being a team or player depending on mode)
        a list of performances (one per performer, being a player).



    A given session has two leaderboard of interest, the state of the game's leaderboard before that
    session was played and after. This is what provides a measure of the impact that session has on a
    leaderboard (the state of the leaderboard before the session is of course its state immediately
    after the previous session of that game. This is porovided by session.leaderboard_impact().

    Given we want to record the impact of that session before and after this logged change we have 2
    such impacts to store (4 leaderboard snapshots). There could be some duplication of data there,
    specifically if the session game and date_time are not rebuild triggering (changed game or date_time
    changed to alter sequence of sessions) then the two before snapshot sin these impacts will be
    identical. In the rather odd case of a logged change that has no impact on the after boards they
    will  be identical too (though hard to imagine a change worth logging that has no impact!).

    TODO: This is a place to store impacts for reporting and presentation for review before
    confirming a commit. That will rely on a transaction manager (in the making) which can hold
    a database transaction open across a few views.

    The idea is that we can store impacts in this model with a key so that they can be calculated,
    saved, and the key passed to a confirmation view where the change can be committed or rolled back.
    '''

    # The session that caused the impact (if it still exists) - if it was deleted it won't be around any more.
    session = models.ForeignKey(Session, verbose_name='Session', related_name='change_logs', null=True, blank=True, on_delete=models.SET_NULL)  # If the session is deleted we may NEED the impact of that!

    # A JSON field that stores a change summary produced by Session.__json__()
    # This shoudl saliently record which fields changed and for those that changed the value before and after the change
    changes = models.TextField(verbose_name='Changes logged for this session', null=True)

    # Any changes to the game a session relates are centrally important to note because
    # it determines whcih leaderboards are impacted. session.game of course identifies the
    # game but that is the game the session points to now (and it may have been edited again
    # after this change). An unlikely scenario of course, but if it happens we want to know
    # and so a log is doubly important.
    game_before_change = models.ForeignKey(Game, null=True, blank=True, related_name='session_before_change_logs', on_delete=models.SET_NULL)
    game_after_change = models.ForeignKey(Game, null=True, blank=True, related_name='session_after_change_logs', on_delete=models.SET_NULL)

    # Space to store 2 JSON leaderboard impacts, one before the change and one after
    leaderboard_impact_before_change = models.TextField(verbose_name='Leaderboard impact before the change', null=True)
    leaderboard_impact_after_change = models.TextField(verbose_name='Leaderboard impact after the change', null=True)

    # True if the logged change impacted a leaderboard
    # i.e. if leaderboard_impact_after_change and leaderboard_impact_before_change are identical
    # The impacts can be large JSON strings and this flag registers our expectation that they are the same
    # That is, that no leaderboard impacting change was made. This is also stored in changes above but this
    # is an extract from that for easy querying and filterig of logs that only have impact or not.Which
    # can be useful for expiring logs that have low utility.
    has_impact = models.BooleanField('This change impacted the leaderboard')

    # If this change triggered a rebuild a pointer to the log of that rebuild.
    rebuild_log = models.ForeignKey('RebuildLog', null=True, on_delete=models.SET_NULL, default=None, related_name='change_logs')
    # change_logs points back to here from RebuildLog

    @property_method
    def Leaderboard_impact_before_change(self, unwrap=None):
        '''
        Returns the leaderboard impact before the change unpacked into a Python tuple.

        Note that before and after have two contexts.
            Before and after teh change that was logged.
            Before and after the logged session was played.
        We endeavour to be clear at every stage which before and after we're talking about.

        Either as a LB_STRUCTURE.game_wrapped_session_wrapped_player_list with LB_PLAYER_LIST_STYLE.data
        or as a LB_STRUCTURE.player_list with LB_PLAYER_LIST_STYLE.data.

        :param unwrap: If "before" or "after" unwraps the before or after player_list
        '''
        if self.leaderboard_impact_before_change:
            igd = LB_STRUCTURE.game_data_element.value
            isd = LB_STRUCTURE.session_data_element.value
            leaderboard = immutable(json.loads(self.leaderboard_impact_before_change))
            has_before = len(leaderboard[igd]) > 1  # The first session added for a game never has a before board!
            if unwrap == "before":
                return leaderboard[igd][0][isd] if has_before else None
            elif unwrap == "after":
                return leaderboard[igd][1][isd] if has_before else leaderboard[igd][0][isd]
            else:
                return leaderboard
        else:
            return None

    @property_method
    def Leaderboard_impact_after_change(self, unwrap=None):
        '''
        Returns the leaderboard impact after the change unpacked into a Python tuple.

        Note that before and after have two contexts.
            Before and after teh change that was logged.
            Before and after the logged session was played.
        We endeavour to be clear at every stage which before and after we're talking about.

        Either as a LB_STRUCTURE.game_wrapped_session_wrapped_player_list with LB_PLAYER_LIST_STYLE.data
        or as a LB_STRUCTURE.player_list with LB_PLAYER_LIST_STYLE.data.

        :param unwrap: If "before" or "after" unwraps the before or after player_list
        '''
        if self.leaderboard_impact_after_change:
            igd = LB_STRUCTURE.game_data_element.value
            isd = LB_STRUCTURE.session_data_element.value
            leaderboard = immutable(json.loads(self.leaderboard_impact_after_change))
            has_before = len(leaderboard[igd]) > 1  # The first session added for a game never has a before board!
            if unwrap == "before":
                return leaderboard[igd][0][isd] if has_before else None
            elif unwrap == "after":
                return leaderboard[igd][1][isd] if has_before else leaderboard[igd][0][isd]
            else:
                return leaderboard
        else:
            return None

    def leaderboard_after(self, game):
        '''
        Returns the leaderboard after session play (in an impact) for the specified game if it is
        in self.Games or identified by "before" or "after" (the change)

        :param game: either a Game instance from self.Games, or "before" or "after"
        '''
        lb_before_change = self.Leaderboard_impact_before_change(unwrap="after")
        lb_after_change = self.Leaderboard_impact_after_change(unwrap="after")

        # We check for the game fter change first (which 99.999% of the time is the same
        # as game before the change anyhow, I mena how often does one enter the game
        # erroneously needing an after edit) becaiuse if the game wasn't changed then
        # it's the after session board after the we want.
        if game == self.game_after_change or game == "after":
            return lb_after_change

        # Only if the game chnaged, are we interested in checking the before
        # change game as well and if we have that one, we want its leaderboard impact.
        elif game == self.game_before_change or game == "before":
            return lb_before_change
        else:
            return None

    @property
    def Changes(self):
        if self.changes:
            return json.loads(self.changes)
        else:
            return None

    @property
    def Games(self):
        '''
        Returns a tuple of game instances affected by the chnage
        '''
        return (self.game_after_change,) if not self.game_before_change or self.game_after_change == self.game_before_change else (self.game_before_change, self.game_after_change)

    @classmethod
    def create(cls, session=None, change_summary=None, rebuild_log=None):
        '''
        Initial creation of a ChangeLog, with the session provided in its before change state.

        Should be called before a submitted form is saved (so the session is in its before change state).

        All args are optional and can be saved now (at time of creation) or later (at time of update).
        Either way is fine.

        :param session:        A Session object, the change to which we are logging.
        :param change_summary: A JSON log of change_summary, as produced by session.__json__(form_data)
        :param rebuild_log:    An instance of RebuildLog (to link to this ChangeLog)
        '''
        # Instantiate a log
        self = cls()

        if session:
            # The change is an edit to an existing session
            self.session = session

            self.game_before_change = session.game
            # Saves in LB_STRUCTURE. game_wrapped_session_wrapped_player_list with LB_PLAYER_LIST_STYLE.data
            self.leaderboard_impact_before_change = json.dumps(session.leaderboard_impact(LB_PLAYER_LIST_STYLE.data), cls=DjangoJSONEncoder)
        else:
            # The change is a session submission (no session object exists yet, if it's created then self.update() can add it)
            pass

        if isinstance(change_summary, str):
            self.changes = change_summary
            changes = json.loads(change_summary).get("changes", [])
            self.has_impact = 'leaderboard' in changes

        if isinstance(rebuild_log, RebuildLog):
            self.rebuild_log = rebuild_log

        # Return the instance
        return self

    def update(self, session=None, change_summary=None, rebuild_log=None):
        '''
        Update an existing ChangeLog with the after change states.

        Should be called after a submitted form is saved.

        Ideally before it is committed but it matters not the ChangeLog stands
        either way as long as it is saved in the same transaction as the submitted
        form and so commits or rolls back in unison with that save.

        change_summary and rebuild_log are optional and can be saved now (at time of update) or earlier (at time of creation).
        Either way is fine.

        :param session:        A Session object, the change to which we are logging.
        :param change_summary:        A JSON log of change_summary, as produced by session.__json__(form_data)
        :param rebuild_log:    An instance of RebuildLog (to link to this ChangeLog)
        '''

        # session need not be supplied if it was at creation time, conversely if it was not
        # Generally if logging a session submission (Create) then we expect to receive it now
        # and if logging a session edit (Update) then we should have the session already (supplied to create() above)
        if session:
            if self.session:
                if not session == self.session:
                    raise ValueError("Attempt to update ChangeLog with session different to the one it was created with.")
            else:
                self.session = session
        else:
            if self.session:
                session = self.session
            else:
                raise ValueError("Attempt to update ChangeLog with no related session.")

        self.game_after_change = session.game
        # Saves in LB_STRUCTURE. game_wrapped_session_wrapped_player_list with LB_PLAYER_LIST_STYLE.data
        self.leaderboard_impact_after_change = json.dumps(session.leaderboard_impact(LB_PLAYER_LIST_STYLE.data), cls=DjangoJSONEncoder)

        if isinstance(change_summary, str):
            self.changes = change_summary
            changes = json.loads(change_summary).get("changes", [])
            self.has_impact = 'leaderboard' in changes

        if isinstance(rebuild_log, RebuildLog):
            self.rebuild_log = rebuild_log

    class Meta(AdminModel.Meta):
        verbose_name = "Change Log"
        verbose_name_plural = "Change Logs"


class RebuildLog(AdminModel):
    '''
    A log of rating rebuilds.

    Kept for two reasons:

    1) Performance measure. Rebuild can be slow and we'd like to know how slow.
    2) Security. To see who rebuilt what when

    Unlike change logs these should be persisted

    When we make any changes to any recorded game Session that is NOT the latest game session (for
    that game and all the players playing in that game session) then it has an impact on the
    leaderboards for that game that is distinct from it's own immediate impact. To clarify:

    When any session is changed it has an immediate impact which is how it alters the leaderboard
    from the immediately prior played session of that game.

    If there are future sessions relative to the session just changed then it also has an impact on
    the current leaderboard. The immediate impact above is not the current leaderboard (because
    other session of that game are in its future) and so the impact on the current leaderboard is
    also useful to see.

    This is true whether a session is added, or altered (in any one of many ratings impacting
    ways: players change, ranks change, game changes etc)or deleted.

    We'd like to show these impacts for any edit before they are committed.

    The current leaderboard impacts are tricky as the current leaderboards could be changing
    while we're reviewing our commit for example (other user submitting results).
    '''

    # AdminModel provides created_by and created_on that record who performed the rebuild when.

    ratings = models.PositiveIntegerField('Number of Ratings Built')
    duration = models.DurationField('Duration of Rebuild', null=True)

    # Record what triggered this rating rebuild. the sessin and reason can provide supporting detail.
    trigger = models.PositiveSmallIntegerField(choices=RATING_REBUILD_TRIGGER.choices.value, default=RATING_REBUILD_TRIGGER.user_request.value, blank=False)

    # The session that triggered the rebuild (if any)
    # Note if any change_logs exist they also identify a session.
    # Note: for the SESSION_DELETE trigger clearly the session no longer exists and cannot be identified (unless we keep trash)
    session = models.ForeignKey(Session, verbose_name='Session', related_name='rebuild_logs', null=True, blank=True, on_delete=models.SET_NULL)
    reason = models.TextField('Reason for Rebuild')

    # A record of the arguments of the rebuild request. These can be null. See Rating.rebuild() which
    # can rebuild all the ratings, or all the ratings for a specific game, or all the ratings from
    # a given time, or the ratings from a given time for a specific game or for a specific list of sessions.
    game = models.ForeignKey('Game', null=True, blank=True, related_name='rating_rebuild_requests', on_delete=models.CASCADE)  # If a game is deleted and this was a game specific log, we can delete it
    date_time_from = models.DateTimeField('Game', null=True, blank=True)
    sessions = models.ManyToManyField('Session', blank=True, related_name='rating_rebuild_requests')

    # We'd like to store JSON leaderboard impact of the rebuild. As the rebuild can cover the whole database this
    # can be large beyond simple database storage, and so we should use fileystem storage!
    rebuild_log_dir = "logs/rating_rebuilds"
    leaderboards_before_rebuild = RelativeFilePathField(path=rebuild_log_dir, null=True, blank=True)
    leaderboards_after_rebuild = RelativeFilePathField(path=rebuild_log_dir, null=True, blank=True)

    @property
    def leaderboards_before(self) -> dict:
        log_file = os.path.join(settings.BASE_DIR, self.leaderboards_before_rebuild)
        try:
            with open(log_file, 'r') as f:
                leaderboards = json.load(f)
        except:
            leaderboards = {}

        return pythonify(leaderboards)

    @property
    def leaderboards_after(self) -> dict:
        log_file = os.path.join(settings.BASE_DIR, self.leaderboards_after_rebuild)
        try:
            with open(log_file, 'r') as f:
                leaderboards = json.load(f)
        except:
            leaderboards = {}

        return pythonify(leaderboards)

    @property
    def games(self):
        '''
        Returns a tuple of game PKs affected by the rebuild
        '''
        return tuple(self.leaderboards_before.keys())

    @property
    def Games(self):
        '''
        Returns a tuple of game instances affected by the rebuild
        '''
        return tuple([safe_get(Game, pk) for pk in self.games])

    @property
    def player_rating_impact(self) -> dict:
        '''
        Returns a dict of dicts keyed on game then player (whose ratings were affected by by this rebuild).
        '''

        # Game.pk keyed dicts of player lists in data style
        before = self.leaderboards_before
        after = self.leaderboards_after

        deltas = {}
        for g in self.Games:
            deltas[g] = {}
            old = player_ratings(before[g.pk], structure=LB_STRUCTURE.player_list, style=LB_PLAYER_LIST_STYLE.data)
            new = player_ratings(after[g.pk], structure=LB_STRUCTURE.player_list, style=LB_PLAYER_LIST_STYLE.data)

            for p in old:
                if not new[p] == old[p]:
                    delta = new[p] - old[p]
                    P = safe_get(Player, p)
                    deltas[g][P] = delta

        return deltas

    @property
    def player_ranking_impact(self) -> dict:
        '''
        Returns a dict of dicts keyed on game then player (whose rankings were affected by by this rebuild).
        '''

        # Game.pk keyed dicts of player lists in data style
        before = self.leaderboards_before
        after = self.leaderboards_after

        deltas = {}
        for g in self.Games:
            deltas[g] = {}
            old = player_rankings(before[g.pk], structure=LB_STRUCTURE.player_list, style=LB_PLAYER_LIST_STYLE.data)
            new = player_rankings(after[g.pk], structure=LB_STRUCTURE.player_list, style=LB_PLAYER_LIST_STYLE.data)

            for p in old:
                if not new[p] == old[p]:
                    delta = new[p] - old[p]
                    P = safe_get(Player, p)
                    deltas[g][P] = delta

        return deltas

    def save_leaderboards(self, games, context):
        '''
        Saves leaderboards (in the "data" style) to a disk file and points the context appropriate FileField to it.

        :param games:    A set or list of one or more games
        :param context:  "before" or "after"
        '''
        if not context in ["before", "after"]:
            raise ValueError(f"RebuildLog.save_leaderboards() context must be 'before' or 'after' but '{context}' was provided.")

        leaderboards = Rating.leaderboards(games, style=LB_PLAYER_LIST_STYLE.data)  # dict of boards keyed on game.pk
        content = json.dumps(leaderboards, indent='\t', cls=DjangoJSONEncoder)

        abs_directory = os.path.join(settings.BASE_DIR, self.rebuild_log_dir)
        filename = f"{self.created_on_local:%Y-%m-%d-%H-%M-%S}-{self.created_by.username}-{self.pk}-{context}.json"
        abs_filename = os.path.join(abs_directory, filename)
        rel_filename = os.path.join(self.rebuild_log_dir, filename)

        # Ensure the directory exists
        os.makedirs(abs_directory, exist_ok=True)

        with open(abs_filename, 'w') as f:
            f.write(content)

        if context == "before":
            self.leaderboards_before_rebuild = rel_filename
        elif context == "after":
            self.leaderboards_after_rebuild = rel_filename

    def leaderboard_before(self, game, wrap=True):
        '''
        Returns the leaderboard for a game as it was before this Rebuild (which was logged).

        Returns LB_STRUCTURE.game_wrapped_player_list with LB_PLAYER_LIST_STYLE.rich
        or LB_STRUCTURE.player_list with LB_PLAYER_LIST_STYLE.data.

        :param game: an instance of Game
        :param wrap: If true, a game wrapped rich player list, else a naked data player list
        '''
        leaderboards = self.leaderboards_before  # dict keyed on game.pk
        player_list = tuple(leaderboards.get(game.pk, []))

        if player_list:
            if wrap:
                structure = LB_STRUCTURE.player_list
                style = LB_PLAYER_LIST_STYLE.rich
                rich_player_list = restyle_leaderboard(player_list, structure=structure, style=style)
                wrapped_game_board = game.wrapped_leaderboard(rich_player_list)
                return immutable(wrapped_game_board)
            else:
                return immutable(player_list)
        else:
            return None

    def leaderboard_after(self, game, wrap=True):
        '''
        Returns the leaderboard for a game as it was after this Rebuild (which was logged).

        Returns LB_STRUCTURE.game_wrapped_player_list with LB_PLAYER_LIST_STYLE.rich
        or LB_STRUCTURE.player_list with LB_PLAYER_LIST_STYLE.data.

        :param game: an instance of Game
        :param wrap: If true, a game wrapped rich player list, else a naked data player list
        '''
        leaderboards = self.leaderboards_after  # dict keyed on game.pk
        player_list = leaderboards.get(game.pk, [])

        if player_list:
            if wrap:
                structure = LB_STRUCTURE.player_list
                style = LB_PLAYER_LIST_STYLE.rich
                rich_player_list = restyle_leaderboard(player_list, structure=structure, style=style)
                wrapped_game_board = game.wrapped_leaderboard(rich_player_list)
                return immutable(wrapped_game_board)
            else:
                return immutable(player_list)
        else:
            return None

    def leaderboard_impact(self, game):
        '''
        Returns leaderboard_after and leaderboard_before as two boards under one game wrapper.

        :param game: an instance of Game
        '''
        after = self.leaderboards_after
        before = self.leaderboards_before

        if after and before:
            after_board = tuple(after.get(game.pk, []))
            before_board = tuple(before.get(game.pk, []))

            structure = LB_STRUCTURE.player_list
            style = LB_PLAYER_LIST_STYLE.rich
            rich_after = restyle_leaderboard(after_board, structure=structure, style=style)
            rich_before = restyle_leaderboard(before_board, structure, style=style)
            rich_after = augment_with_deltas(rich_after, rich_before, structure=structure)

            return game.wrapped_leaderboard([rich_after, rich_before], snap=True)
        else:
            return None

    @property
    def leaderboards_impact(self) -> dict:
        '''
        Returns a Game PK keyed dict of leaderboard impacts (of the rebuild)
        '''
        impacts = {}
        for game in self.Games:
            impacts[game.pk] = self.leaderboard_impact(game)
        return impacts

    class Meta(AdminModel.Meta):
        verbose_name = "Rebuild Log"
        verbose_name_plural = "Rebuild Logs"

