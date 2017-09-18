from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.urlresolvers import reverse_lazy
from django.core.validators import MinValueValidator, MaxValueValidator
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import string_concat

from localflavor.us.models import PhoneNumberField
from localflavor.us.models import USStateField
from ws.fields import OptionalOneToOneField
import ws.utils.dates as dateutils


pytz_timezone = timezone.get_default_timezone()


alphanum = RegexValidator(r'^[a-zA-Z0-9 ]*$',
                          "Only alphanumeric characters and spaces allowed")


class SingletonModel(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.pk = 1
        super(SingletonModel, self).save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        obj, created = cls.objects.get_or_create(pk=1)
        return obj


class Car(models.Model):
    # As long as this module is reloaded once a year, this is fine
    # (First license plates were issued in Mass in 1903)
    year_min, year_max = 1903, dateutils.local_now().year + 2
    # Loosely validate - may wish to use international plates in the future
    license_plate = models.CharField(max_length=31, validators=[alphanum])
    state = USStateField()
    make = models.CharField(max_length=63)
    model = models.CharField(max_length=63)
    year = models.PositiveIntegerField(validators=[MaxValueValidator(year_max),
                                                   MinValueValidator(year_min)])
    color = models.CharField(max_length=63)

    def __unicode__(self):
        car_info = "{} {} {} {}".format(self.color, self.year, self.make, self.model)
        registration_info = "-".join([self.license_plate, self.state])
        return "{} ({})".format(car_info, registration_info)


class EmergencyContact(models.Model):
    name = models.CharField(max_length=255)
    cell_phone = PhoneNumberField()
    relationship = models.CharField(max_length=63)
    email = models.EmailField()

    def __unicode__(self):
        return "{} ({}): {}".format(self.name, self.relationship, self.cell_phone)


class EmergencyInfo(models.Model):
    emergency_contact = models.OneToOneField(EmergencyContact)
    allergies = models.CharField(max_length=255)
    medications = models.CharField(max_length=255)
    medical_history = models.TextField(max_length=2000,
                                       help_text="Anything your trip leader would want to know about.")

    def __unicode__(self):
        return ("Allergies: {} | Medications: {} | History: {} | "
                "Contact: {}".format(self.allergies, self.medications,
                                     self.medical_history, self.emergency_contact))


class LeaderManager(models.Manager):
    def get_queryset(self):
        all_participants = super(LeaderManager, self).get_queryset()
        leaders = all_participants.filter(leaderrating__active=True).distinct()
        return leaders.prefetch_related('leaderrating_set')


class Discount(models.Model):
    """ Discount at another company available to MITOC members. """
    administrators = models.ManyToManyField('Participant', blank=True, help_text="Persons selected to administer this discount",
                                            related_name='discounts_administered')

    active = models.BooleanField(default=True, help_text="Discount is currently open & active")
    name = models.CharField(max_length=255)
    summary = models.CharField(max_length=255)
    terms = models.TextField(max_length=4095)
    url = models.URLField(null=True, blank=True)
    ga_key = models.CharField(max_length=63, help_text="key for Google spreadsheet with membership information (shared as read-only with the company)")

    time_created = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)

    student_required = models.BooleanField(default=False, help_text="Discount provider requires recipients to be students")

    report_school = models.BooleanField(default=False, help_text="Report MIT affiliation if participant is a student")
    report_student = models.BooleanField(default=False, help_text="Report MIT affiliation and student status to discount provider")
    report_leader = models.BooleanField(default=False, help_text="Report MITOC leader status to discount provider")
    report_access = models.BooleanField(default=False, help_text="Report if participant should have leader, student, or admin level access")

    def __unicode__(self):
        return self.name


class Participant(models.Model):
    """ Anyone going on a trip needs WIMP info, and info about their car.

    Even leaders will have a Participant record (see docstring of LeaderRating).
    """
    user_id = models.IntegerField()  # Technically a FK, but to another DB

    objects = models.Manager()
    leaders = LeaderManager()
    name = models.CharField(max_length=255)
    cell_phone = PhoneNumberField(null=True, blank=True)  # Hi, Sheep.
    last_updated = models.DateTimeField(auto_now=True)
    emergency_info = models.OneToOneField(EmergencyInfo)
    email = models.EmailField(unique=True, help_text=string_concat("This will be shared with leaders & other participants. <a href='",
                                                                   reverse_lazy('account_email'),
                                                                   "'>Manage email addresses</a>."))
    car = OptionalOneToOneField(Car)

    # We used to not collect level of student + MIT affiliation
    # Any participants with single-digit affiliation codes have dated status
    # Old codes were: S (student), M (MIT affiliate), and N (non-affiliated)
    affiliation = models.CharField(
        max_length=2,
        choices=[
            ('Undergraduate student', [('MU', "MIT undergrad"), ('NU', "Non-MIT undergrad")]),
            ('Graduate student', [('MG', "MIT grad student"), ('NG', "Non-MIT grad student")]),
            ('MA', 'MIT affiliate'),
            ('NA', 'Non-affiliate'),
        ]
    )
    STUDENT_AFFILIATIONS = {'MU', 'NU', 'MG', 'NG'}

    discounts = models.ManyToManyField(Discount, blank=True)

    @property
    def is_student(self):
        return self.affiliation in self.STUDENT_AFFILIATIONS

    @property
    def user(self):
        return User.objects.prefetch_related('groups').get(pk=self.user_id)

    def ratings(self, rating_active=True, at_time=None, after_time=None):
        """ Return all ratings matching the supplied filters.

        rating_active: Only format a rating if it's still active
        at_time:       Only return current ratings before the given time
                       (useful to get a past, but not necessarily current, rating)
        after_time:    Only return ratings created after this time
        """
        # (We do this in raw Python instead of `filter()` to avoid n+1 queries
        # This method should be called when leaderrating_set was prefetched
        ratings = (r for r in self.leaderrating_set.all()
                   if r.active or not rating_active)
        if at_time:
            ratings = (r for r in ratings if r.time_created <= at_time)
        if after_time:
            ratings = (r for r in ratings if r.time_created > after_time)
        return ratings

    def name_with_rating(self, trip):
        """ Give the leader's name plus rating at the time of the trip.

        Note: Some leaders from Winter School 2014 or 2015 may not have any
        ratings. In those years, we deleted all Winter School ratings at the
        end of the season (so leaders who did not return the next year lost
        their ratings).

        If no rating is found, simply the name will be given.
        """
        kwargs = {'at_time': trip.midnight_before, 'rating_active': False}
        rating = self.activity_rating(trip.activity, **kwargs)
        return "{} ({})".format(self.name, rating) if rating else self.name

    def activity_rating(self, activity, **kwargs):
        """ Return leader's rating for the given activity (if one exists). """
        ratings = [r for r in self.ratings(**kwargs) if r.activity == activity]
        if not ratings:
            return None
        return max(ratings, key=lambda rating: rating.time_created).rating

    @property
    def allowed_activities(self):
        active_ratings = self.leaderrating_set.filter(active=True)
        activities = active_ratings.values_list('activity', flat=True)
        if activities:
            return list(activities) + LeaderRating.OPEN_ACTIVITIES
        else:  # Not a MITOC leader, can't lead anything
            return []

    def can_lead(self, activity):
        """ Can participant lead trips of the given activity type. """
        if activity in LeaderRating.OPEN_ACTIVITIES and self.is_leader:
            return True
        return self.leaderrating_set.filter(activity=activity, active=True).exists()

    @property
    def is_leader(self):
        """ Query ratings to determine if this participant is a leader.

        Wnen dealing with Users, it's faster to use utils.perms.is_leader
        """
        return self.leaderrating_set.filter(active=True).exists()

    @property
    def email_addr(self):
        return '"{}" <{}>'.format(self.name, self.email)

    @property
    def info_current(self):
        since_last_update = timezone.now() - self.last_updated
        return since_last_update.days < settings.MUST_UPDATE_AFTER_DAYS

    def __unicode__(self):
        return self.name

    class Meta:
        ordering = ['name', 'email']


class LectureAttendance(models.Model):
    year = models.PositiveIntegerField(validators=[MinValueValidator(2016)],
                                       default=dateutils.ws_year,
                                       help_text="Winter School year when lectures were attended.")
    participant = models.ForeignKey(Participant)
    creator = models.ForeignKey(Participant, related_name='lecture_attendances_marked')
    time_created = models.DateTimeField(auto_now_add=True)


class WinterSchoolSettings(SingletonModel):
    """ Stores settings for the current Winter School.

    These settings should only be modified by the WS chair.
    """
    time_created = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)
    last_updated_by = models.ForeignKey(Participant, null=True, blank=True)
    allow_setting_attendance = models.BooleanField(default=False, verbose_name="Let participants set lecture attendance")


class BaseRating(models.Model):
    # Activities where you must be rated in order to create/lead a trip
    BIKING = 'biking'
    BOATING = 'boating'
    CABIN = 'cabin'
    CLIMBING = 'climbing'
    HIKING = 'hiking'
    WINTER_SCHOOL = 'winter_school'
    CLOSED_ACTIVITY_CHOICES = [
        (BIKING, 'Biking'),
        (BOATING, 'Boating'),
        (CABIN, 'Cabin'),
        (CLIMBING, 'Climbing'),
        (HIKING, 'Hiking'),
        (WINTER_SCHOOL, 'Winter School'),
    ]
    CLOSED_ACTIVITIES = [val for (val, label) in CLOSED_ACTIVITY_CHOICES]

    # Activities for which a specific leader rating is not necessary
    # (You must be a MITOC leader, but not necessarily in these activities)
    CIRCUS = 'circus'
    OFFICIAL_EVENT = 'official_event'  # Training, films, etc.
    COURSE = 'course'
    OPEN_ACTIVITY_CHOICES = [
        (CIRCUS, 'Circus'),
        (OFFICIAL_EVENT, 'Official Event'),
        (COURSE, 'Course'),
    ]

    OPEN_ACTIVITIES = [val for (val, label) in OPEN_ACTIVITY_CHOICES]

    ACTIVITIES = CLOSED_ACTIVITIES + OPEN_ACTIVITIES
    ACTIVITY_CHOICES = CLOSED_ACTIVITY_CHOICES + OPEN_ACTIVITY_CHOICES

    time_created = models.DateTimeField(auto_now_add=True)
    participant = models.ForeignKey(Participant)
    activity = models.CharField(max_length=31, choices=ACTIVITY_CHOICES)
    rating = models.CharField(max_length=31)
    notes = models.TextField(max_length=500, blank=True)  # Contingencies, etc.

    def __unicode__(self):
        return "{} ({}, {})".format(self.participant, self.rating, self.activity)

    class Meta:
        abstract = True
        ordering = ["participant"]


class LeaderRating(BaseRating):
    """ A leader is just a participant with ratings for at least one activity type.

    The same personal + emergency information is required of leaders, but
    additional fields are present. So, we keep a Participant record for any
    leader. This makes it easy to check if any participant is a leader (just
    see `participant.ratings`) and easy to promote somebody to leader.

    It also allows leaders to function as participants (e.g. if a "SC" leader
    wants to go ice climbing).
    """
    creator = models.ForeignKey(Participant, related_name='ratings_created')
    active = models.BooleanField(default=True)


class LeaderRecommendation(BaseRating):
    creator = models.ForeignKey(Participant, related_name='recommendations_created')


class BaseSignUp(models.Model):
    participant = models.ForeignKey(Participant)
    trip = models.ForeignKey("Trip")
    time_created = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)
    notes = models.TextField(blank=True, max_length=1000)  # e.g. Answers to questions

    class Meta:
        abstract = True

    def __unicode__(self):
        return "{} on {}".format(self.participant.name, self.trip)


class LeaderSignUp(BaseSignUp):
    """ Represents a leader who has signed up to join a trip. """
    class Meta:
        ordering = ["time_created"]


class SignUp(BaseSignUp):
    """ An editable record relating a Participant to a Trip.

    The time of creation determines ordering in first-come, first-serve.
    """
    order = models.IntegerField(null=True, blank=True)  # As ranked by participant
    manual_order = models.IntegerField(null=True, blank=True)  # Order on trip

    on_trip = models.BooleanField(default=False)

    @property
    def other_signups(self):
        """ Return other relevant signups for this participant.

        The point of this property is to help leaders coordinate driving.
        """
        on_trips = self.__class__.objects.filter(on_trip=True,
                                                 participant=self.participant)
        others = on_trips.exclude(trip=self.trip)
        within_three_days = (self.trip.trip_date - timedelta(days=3),
                             self.trip.trip_date + timedelta(days=3))
        return others.filter(trip__trip_date__range=within_three_days)

    def save(self, **kwargs):
        """ Assert that the Participant is not signing up twice.

        The AssertionError here should never be thrown - it's a last defense
        against a less-than-obvious implementation of adding Participant
        records after getting a bound form.
        """
        if not kwargs.pop('commit', True):
            assert self.trip not in self.participant.trip_set.all()
        super(SignUp, self).save(**kwargs)

    class Meta:
        # When ordering for an individual, should order by priority (i.e. 'order')
        # When ordering for many, should go by time created.
        ordering = ["manual_order", "last_updated"]


class TripInfo(models.Model):
    drivers = models.ManyToManyField(Participant, blank=True,
                                     help_text=string_concat("If a trip participant is driving, but is not on this list, they must first submit <a href='",
                                                             reverse_lazy('edit_profile'),
                                                             "#car'>information about their car</a>. They should then be added here."))
    start_location = models.CharField(max_length=127)
    start_time = models.CharField(max_length=63)
    turnaround_time = models.CharField(max_length=63, blank=True,
                                       help_text="The time at which you'll turn back and head for your car/starting location")
    return_time = models.CharField(max_length=63, help_text="When you expect to return to your car/starting location and be able to call the WIMP")
    worry_time = models.CharField(max_length=63, help_text="Suggested: return time +3 hours. If the WIMP has not heard from you after this time and is unable to make contact with any leaders or participants, the authorities will be called.")
    itinerary = models.TextField(help_text="A detailed account of your trip plan. Where will you be going? What route will you be taking? "
                                           "Include trails, peaks, intermediate destinations, back-up plans- anything that would help rescuers find you.")


class Trip(models.Model):
    activity = models.CharField(max_length=31,
                                choices=LeaderRating.ACTIVITY_CHOICES,
                                default=LeaderRating.WINTER_SCHOOL)
    creator = models.ForeignKey(Participant, related_name='created_trips')
    # Leaders should be privileged at time of trip creation, but may no longer
    # be leaders later (and we don't want to break the relation)
    leaders = models.ManyToManyField(Participant, related_name='trips_led', blank=True)
    allow_leader_signups = models.BooleanField(default=False,
                                               help_text="Allow leaders to sign themselves up as trip leaders. (Leaders can always sign up as participants). Recommended for Circuses!")
    name = models.CharField(max_length=127)
    description = models.TextField()
    maximum_participants = models.PositiveIntegerField(default=8, verbose_name="Max participants")
    difficulty_rating = models.CharField(max_length=63)
    level = models.CharField(max_length=255, help_text="This trip's A, B, or C designation (plus I/S rating if applicable).", null=True, blank=True)
    prereqs = models.CharField(max_length=255, blank=True, verbose_name="Prerequisites")
    chair_approved = models.BooleanField(default=False)
    notes = models.TextField(blank=True, max_length=2000,
                             help_text="Participants must add notes to their signups if you complete this field. "
                                       "This is a great place to ask important questions.")

    time_created = models.DateTimeField(auto_now_add=True)
    last_edited = models.DateTimeField(auto_now=True)
    trip_date = models.DateField(default=dateutils.nearest_sat)
    signups_open_at = models.DateTimeField(default=timezone.now)
    signups_close_at = models.DateTimeField(default=dateutils.wed_morning, null=True, blank=True)

    let_participants_drop = models.BooleanField(default=False,
                                                help_text="Allow participants to remove themselves from the trip any time before its start date.")
    honor_participant_pairing = models.BooleanField(default=True,
                                                    help_text="Try to place paired participants together on the trip.")

    info = OptionalOneToOneField(TripInfo)

    signed_up_participants = models.ManyToManyField(Participant, through=SignUp)
    algorithm = models.CharField(max_length=31, default='lottery',
                                 choices=[('lottery', 'lottery'),
                                          ('fcfs', 'first-come, first-serve')])

    lottery_task_id = models.CharField(max_length=36, unique=True, null=True, blank=True)
    lottery_log = models.TextField(null=True, blank=True)

    def __unicode__(self):
        return self.name

    @property
    def single_trip_pairing(self):
        """ Return if the trip will apply pairing as a single lottery trip. """
        if self.algorithm != "lottery":
            return False  # Trip is FCFS, or lottery has completed
        if self.activity == LeaderRating.WINTER_SCHOOL:
            return False  # Winter School trips do their own lottery
        return self.honor_participant_pairing

    @property
    def in_past(self):
        return self.trip_date < dateutils.local_date()

    @property
    def upcoming(self):
        return self.trip_date > dateutils.local_date()

    @property
    def after_lottery(self):
        """ True if it's after the lottery, but takes place before the next one. """
        next_lottery = dateutils.next_lottery()
        past_lottery = dateutils.lottery_time(next_lottery - timedelta(days=7))

        return (dateutils.local_now() > past_lottery and
                self.midnight_before < next_lottery)

    @property
    def midnight_before(self):
        day_before = self.trip_date - timedelta(days=1)
        return pytz_timezone.localize(dateutils.late_at_night(day_before))

    @property
    def fcfs_close_time(self):
        return pytz_timezone.localize(dateutils.fcfs_close_time(self.trip_date))

    @property
    def open_slots(self):
        accepted_signups = self.signup_set.filter(on_trip=True)
        return self.maximum_participants - accepted_signups.count()

    @property
    def signups_open(self):
        """ If signups are currently open. """
        return self.signups_opened and not self.signups_closed

    @property
    def signups_opened(self):
        """ If signups opened at some time in the past.

        They may have since closed!
        """
        return timezone.now() > self.signups_open_at

    @property
    def signups_closed(self):
        """ If a close time is given, return if that time is passed. """
        return self.signups_close_at and timezone.now() > self.signups_close_at

    @property
    def signups_not_yet_open(self):
        """ True if signups open at some point in the future, else False. """
        return timezone.now() < self.signups_open_at

    @property
    def last_of_priority(self):
        """ The 'manual_order' value for a signup to be priority, but below others.

        That is, leader-ordered signups should go above other signups. (Let's
        say that a leader is organizing signups, but new signups come in before
        they submit the ordering - we want to be sure all their ordering goes
        above any new signups).
        """
        last_signup = self.signup_set.last()
        min_order = last_signup.manual_order or 0
        return min_order + 1

    def make_fcfs(self, signups_open_at=None):
        """ Set the algorithm to FCFS, adjust signup times appropriately. """
        self.algorithm = 'fcfs'
        now = dateutils.local_now()
        if signups_open_at:
            self.signups_open_at = signups_open_at
        elif dateutils.wed_morning() <= now < dateutils.closest_wed_at_noon():
            # If posted between lottery time and noon, make it open at noon
            self.signups_open_at = dateutils.closest_wed_at_noon()
        else:
            self.signups_open_at = now

        # If a Winter School trip is last-minute, occurring mid-week,
        # we end up with a close time in the past (preventing trip creation)
        if self.fcfs_close_time > now:
            self.signups_close_at = self.fcfs_close_time

    def clean(self):
        """ Ensure that all trip dates are reasonable. """
        if not self.time_created:  # Trip first being created
            is_ws_trip = self.activity == LeaderRating.WINTER_SCHOOL
            if is_ws_trip and self.after_lottery:
                self.make_fcfs()

            if self.signups_closed:
                raise ValidationError("Signups can't be closed already!")
            # Careful here - don't want to disallow editing of past trips
            if self.trip_date < dateutils.local_date():
                raise ValidationError("Trips can't occur in the past!")

        close_time = self.signups_close_at
        if close_time and close_time < self.signups_open_at:
            raise ValidationError("Trips cannot open after they close.")

    def leaders_with_rating(self):
        """ All leaders with the rating they had at the time of the trip. """
        return [leader.name_with_rating(self) for leader in self.leaders.all()]

    class Meta:
        ordering = ["-trip_date", "-time_created"]


class BygonesManager(models.Manager):
    def get_queryset(self):
        feedback = super(BygonesManager, self).get_queryset()
        fuggedaboutit = dateutils.local_now() - timedelta(days=390)

        return feedback.exclude(trip__trip_date__lt=fuggedaboutit)


class Feedback(models.Model):
    """ Feedback given for a participant on one trip. """
    objects = BygonesManager()  # By default, ignore feedback older than ~13 months
    everything = models.Manager()  # But give the option to look at older feedback

    participant = models.ForeignKey(Participant)
    leader = models.ForeignKey(Participant, related_name="authored_feedback")
    showed_up = models.BooleanField(default=True)
    comments = models.TextField(max_length=2000)
    # Allows general feedback (i.e. not linked to a trip)
    trip = models.ForeignKey(Trip, null=True, blank=True)
    time_created = models.DateTimeField(auto_now_add=True)

    def __unicode__(self):
        return '{}: "{}" - {}'.format(self.participant, self.comments, self.leader)

    class Meta:
        ordering = ["participant", "-time_created"]


class LotteryInfo(models.Model):
    """ Persists from week-to-week, but can be changed. """
    participant = models.OneToOneField(Participant)
    car_status = models.CharField(max_length=7,
                                  choices=[("none", "Not driving"),
                                           ("own", "Can drive own car"),
                                           ("rent", "Willing to rent")],
                                  default="none")
    number_of_passengers = models.PositiveIntegerField(null=True, blank=True,
                                                       validators=[MaxValueValidator(13, message="Do you drive a bus?")])
    last_updated = models.DateTimeField(auto_now=True)
    paired_with = models.ForeignKey(Participant, null=True, blank=True,
                                    related_name='paired_by')

    @property
    def is_driver(self):
        return self.car_status in ['own', 'rent']

    def clean(self):
        # Renters might not yet know number of passengers
        if self.car_status == 'own' and not self.number_of_passengers:
            raise ValidationError("How many passengers can you bring?")

    class Meta:
        ordering = ["car_status", "number_of_passengers"]


class WaitListSignup(models.Model):
    """ Intermediary between initial signup and the trip's waiting list. """
    signup = models.OneToOneField(SignUp)
    waitlist = models.ForeignKey("WaitList")
    time_created = models.DateTimeField(auto_now_add=True)
    # Specify to override ordering by time created
    manual_order = models.IntegerField(null=True, blank=True)

    def __unicode__(self):
        return "{} waitlisted on {}".format(self.signup.participant.name,
                                            self.signup.trip)

    class Meta:
        # None will come after after integer in reverse sorted,
        # So anyone with a manual ordering integer will be first
        ordering = ["-manual_order", "time_created"]


class WaitList(models.Model):
    """ Treat the waiting list as a simple FIFO queue. """
    trip = models.OneToOneField(Trip)
    unordered_signups = models.ManyToManyField(SignUp, through=WaitListSignup)

    @property
    def signups(self):
        # Don't know of any way to apply this ordering to signups field
        return self.unordered_signups.order_by('waitlistsignup')

    @property
    def first_of_priority(self):
        """ The 'manual_order' value to be first in the waitlist. """
        first_wl_signup = self.waitlistsignup_set.first()
        max_order = first_wl_signup.manual_order or 9  # Could be None
        return max_order + 1

    @property
    def last_of_priority(self):
        """ The 'manual_order' value to be priority, but below others. """
        last_wl_signup = self.waitlistsignup_set.last()
        min_order = last_wl_signup.manual_order or 11  # Could be None
        return min_order - 1


class LeaderApplication(models.Model):
    """ Abstract parent class for all leader applications (doubles as a factory)

    To create a new leader application, write the class:

       <Activity>LeaderApplication (naming matters, see code comments)
    """
    participant = models.ForeignKey(Participant)
    time_created = models.DateTimeField(auto_now_add=True)
    previous_rating = models.CharField(max_length=255, blank=True,
                                       help_text="Previous rating (if any)")
    #desired_rating = ... (a CharField, but can vary per application)
    year = models.PositiveIntegerField(validators=[MinValueValidator(2014)],
                                       default=dateutils.ws_year,
                                       help_text="Year this application pertains to.")

    @property
    def rating_given(self):
        """ Return any activity rating created after this application. """
        return self.participant.activity_rating(self.activity, rating_active=True,
                                                after_time=self.time_created)

    @property
    def application_year(self):
        if self.activity == LeaderRating.WINTER_SCHOOL:
            return dateutils.ws_year()
        else:
            return dateutils.local_date().year

    @staticmethod
    def can_apply(activity):
        """ Return if an application exists for the activity. """
        if activity not in LeaderRating.CLOSED_ACTIVITIES:
            return False
        return bool(LeaderApplication.model_from_activity(activity))

    @property
    def activity(self):
        """ Extract the activity name from the class name/db_name.

        Meant to be used by inheriting classes, for example:
            WinterSchoolLeaderApplication -> 'winter_school'
            ClimbingLeaderApplication -> 'climbing'

        If any class wants to break the naming convention, they should
        set db_name to be the activity without underscores.
        """
        model_name = ContentType.objects.get_for_model(self).model
        activity = model_name[:model_name.rfind('leaderapplication')]
        return 'winter_school' if activity == 'winterschool' else activity

    @staticmethod
    def model_from_activity(activity):
        """ Get the specific inheriting child from the activity.

        Inverse of activity().
        """
        model = ''.join(activity.split('_')).lower() + 'leaderapplication'
        try:
            content_type = ContentType.objects.get(app_label="ws", model=model)
        except ContentType.DoesNotExist:
            return None
        else:
            return content_type.model_class()

    @classmethod
    def from_activity(cls, activity):
        """ Factory for returning appropriate application type. """
        model = cls.model_from_activity(activity)
        return super(LeaderApplication, cls).__new__(model) if model else None

    def __unicode__(self):
        return self.participant.name

    class Meta:
        # Important!!! Child classes must be named: <activity>LeaderApplication
        abstract = True   # See model_from_activity for more
        ordering = ["time_created"]


class HikingLeaderApplication(LeaderApplication):
    desired_rating = models.CharField(max_length=10,
                                      choices=[("Leader", "Leader"),
                                               ("Co-Leader", "Co-Leader")],
                                      help_text="Co-Leader: Can co-lead a 3-season hiking trip with a Leader. Leader: Can run 3-season hiking trips.")

    mitoc_experience = models.TextField(max_length=5000,
                                        verbose_name="Hiking Experience with MITOC",
                                        help_text="How long have you been a MITOC member? Please indicate what official MITOC hikes and Circuses you have been on. Include approximate dates and locations, number of participants, trail conditions, type of trip, etc. Give details of whether you participated, led, or co-led these trips. [Optional]: If you like, briefly summarize your experience on unofficial trips or experience outside of New England.")
    formal_training = models.TextField(blank=True, max_length=5000,
                                       help_text="Please give details of any medical training and qualifications, with dates. Also include any other formal outdoor education or qualifications.")
    leadership_experience = models.TextField(blank=True, max_length=5000,
                                             verbose_name="Group outdoor/leadership experience",
                                             help_text="If you've been a leader elsewhere, please describe that here. This could include leadership in other collegiate outing clubs, student sports clubs, NOLS, Outward Bound, or AMC; working as a guide, summer camp counselor, or Scout leader; or organizing hikes with friends.")


class WinterSchoolLeaderApplication(LeaderApplication):
    # Leave ratings long for miscellaneous comments
    # (Omitted from base - some activities might not have users request ratings)
    desired_rating = models.CharField(max_length=255)

    taking_wfa = models.CharField(max_length=10,
                                  choices=[("Yes", "Yes"),
                                           ("No", "No"),
                                           ("Maybe", "Maybe/don't know")],
                                  verbose_name="Do you plan on taking the subsidized WFA at MIT?",
                                  help_text="Save $110 on the course fee by leading two or more trips!")
    training = models.TextField(blank=True, max_length=5000,
                                verbose_name="Formal training and qualifications",
                                help_text="Details of any medical, technical, or leadership training and qualifications relevant to the winter environment. State the approximate dates of these activities. Leave blank if not applicable.")
    winter_experience = models.TextField(blank=True, max_length=5000,
                                         help_text="Details of previous winter outdoors experience. Include the type of trip (x-country skiiing, above treeline, snowshoeing, ice climbing, etc), approximate dates and locations, numbers of participants, notable trail and weather conditions. Please also give details of whether you participated, lead, or co-lead these trips.")
    other_experience = models.TextField(blank=True, max_length=5000,
                                        verbose_name="Other outdoors/leadership experience",
                                        help_text="Details about any relevant non-winter experience")
    notes_or_comments = models.TextField(blank=True, max_length=5000,
                                         help_text="Any relevant details, such as any limitations on availability on Tue/Thurs nights or weekends during IAP.")


class ClimbingLeaderApplication(LeaderApplication):
    FAMILIARITY_CHOICES = [
        ('none', "not at all"),
        ('some', "some exposure", ),
        ('comfortable', "comfortable"),
        ('very comfortable', "very comfortable"),
    ]

    desired_rating = models.CharField(max_length=32,
                                      choices=[
                                          ("Bouldering", "Bouldering"),
                                          ("Single-pitch", "Single-pitch"),
                                          ("Multi-pitch", "Multi-pitch"),
                                          ("Bouldering + Single-pitch", "Bouldering + Single-pitch"),
                                          ("Bouldering + Multi-pitch", "Bouldering + Multi-pitch"),
                                      ])
    years_climbing = models.IntegerField()
    years_climbing_outside = models.IntegerField()
    outdoor_bouldering_grade = models.CharField(max_length=255, help_text="At what grade are you comfortable bouldering outside?")
    outdoor_sport_leading_grade = models.CharField(max_length=255, help_text="At what grade are you comfortable leading outside on sport routes?")
    outdoor_trad_leading_grade = models.CharField(max_length=255, help_text="At what grade are you comfortable leading outside on trad routes?")

    # How familiar are you with the following...
    familiarity_spotting = models.CharField(max_length=16, choices=FAMILIARITY_CHOICES,
                                            verbose_name="Familarity with spotting boulder problems")
    familiarity_bolt_anchors = models.CharField(max_length=16, choices=FAMILIARITY_CHOICES,
                                                verbose_name="Familiarity with 2-bolt 'sport' anchors")
    familiarity_gear_anchors = models.CharField(max_length=16, choices=FAMILIARITY_CHOICES,
                                                verbose_name="Familiarity with trad 'gear' anchors")
    familiarity_sr = models.CharField(max_length=16, choices=FAMILIARITY_CHOICES,
                                      verbose_name="Familiarity with multi-pitch self-rescue")

    spotting_description = models.TextField(blank=True, help_text="Describe how you would spot a climber on a meandering tall bouldering problem.")
    tr_anchor_description = models.TextField(blank=True, verbose_name="Top rope anchor description", help_text="Describe how you would build a top-rope anchor at a sport crag.")
    rappel_description = models.TextField(blank=True, help_text="Describe how you would set up a safe rappel.")
    gear_anchor_description = models.TextField(blank=True, help_text="Describe what you look for when building a typical gear anchor.")

    formal_training = models.TextField(blank=True)
    teaching_experience = models.TextField(blank=True)
    notable_climbs = models.TextField(blank=True, help_text="What are some particularly memorable climbs you have done?")
    favorite_route = models.TextField(blank=True, help_text="Do you have a favorite route? If so, what is it and why?")

    extra_info = models.TextField(blank=True, help_text="Is there anything else you would like us to know?")
