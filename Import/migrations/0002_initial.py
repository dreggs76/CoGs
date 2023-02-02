# Generated by Django 4.0.3 on 2022-11-25 09:54

import Import.models
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import timezone_field.fields


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('Leaderboards', '0002_alter_session_options_alter_backuprating_created_by_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('Import', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Import',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_on', models.DateTimeField(editable=False, null=True, verbose_name='Time of Creation')),
                ('created_on_tz', timezone_field.fields.TimeZoneField(default='Australia/Hobart', editable=False, verbose_name='Time of Creation, Timezone')),
                ('last_edited_on', models.DateTimeField(editable=False, null=True, verbose_name='Time of Last Edit')),
                ('last_edited_on_tz', timezone_field.fields.TimeZoneField(default='Australia/Hobart', editable=False, verbose_name='Time of Last Edit, Timezone')),
                ('filename', models.CharField(max_length=128)),
                ('file', models.FileField(upload_to=Import.models.local_path)),
                ('complete', models.BooleanField(default=False)),
            ],
            options={
                'verbose_name': 'Import',
                'verbose_name_plural': 'Imports',
                'ordering': ['-created_on'],
                'get_latest_by': ['created_on'],
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='PlayerMap',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_on', models.DateTimeField(editable=False, null=True, verbose_name='Time of Creation')),
                ('created_on_tz', timezone_field.fields.TimeZoneField(default='Australia/Hobart', editable=False, verbose_name='Time of Creation, Timezone')),
                ('last_edited_on', models.DateTimeField(editable=False, null=True, verbose_name='Time of Last Edit')),
                ('last_edited_on_tz', timezone_field.fields.TimeZoneField(default='Australia/Hobart', editable=False, verbose_name='Time of Last Edit, Timezone')),
                ('theirs', models.CharField(max_length=256, verbose_name='Their Player')),
                ('created_by', models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(class)ss_created', to=settings.AUTH_USER_MODEL, verbose_name='Created By')),
                ('last_edited_by', models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(class)ss_last_edited', to=settings.AUTH_USER_MODEL, verbose_name='Last Edited By')),
                ('ours', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='import_maps', to='Leaderboards.player', verbose_name='Our Player')),
                ('related_import', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='player_maps', to='Import.import', verbose_name='Import')),
            ],
            options={
                'get_latest_by': 'created_on',
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='LocationMap',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_on', models.DateTimeField(editable=False, null=True, verbose_name='Time of Creation')),
                ('created_on_tz', timezone_field.fields.TimeZoneField(default='Australia/Hobart', editable=False, verbose_name='Time of Creation, Timezone')),
                ('last_edited_on', models.DateTimeField(editable=False, null=True, verbose_name='Time of Last Edit')),
                ('last_edited_on_tz', timezone_field.fields.TimeZoneField(default='Australia/Hobart', editable=False, verbose_name='Time of Last Edit, Timezone')),
                ('theirs', models.CharField(max_length=256, verbose_name='Their Location')),
                ('created_by', models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(class)ss_created', to=settings.AUTH_USER_MODEL, verbose_name='Created By')),
                ('last_edited_by', models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(class)ss_last_edited', to=settings.AUTH_USER_MODEL, verbose_name='Last Edited By')),
                ('ours', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='import_maps', to='Leaderboards.location', verbose_name='Our Location')),
                ('related_import', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='location_maps', to='Import.import', verbose_name='Import')),
            ],
            options={
                'get_latest_by': 'created_on',
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='ImportContext',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_on', models.DateTimeField(editable=False, null=True, verbose_name='Time of Creation')),
                ('created_on_tz', timezone_field.fields.TimeZoneField(default='Australia/Hobart', editable=False, verbose_name='Time of Creation, Timezone')),
                ('last_edited_on', models.DateTimeField(editable=False, null=True, verbose_name='Time of Last Edit')),
                ('last_edited_on_tz', timezone_field.fields.TimeZoneField(default='Australia/Hobart', editable=False, verbose_name='Time of Last Edit, Timezone')),
                ('name', models.CharField(max_length=256, verbose_name='Name of the Session Import Context')),
                ('created_by', models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(class)ss_created', to=settings.AUTH_USER_MODEL, verbose_name='Created By')),
                ('editors', models.ManyToManyField(related_name='import_contexts', to=settings.AUTH_USER_MODEL, verbose_name='Contexts')),
                ('last_edited_by', models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(class)ss_last_edited', to=settings.AUTH_USER_MODEL, verbose_name='Last Edited By')),
            ],
            options={
                'get_latest_by': 'created_on',
                'abstract': False,
            },
        ),
        migrations.AddField(
            model_name='import',
            name='context',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='imports', to='Import.importcontext', verbose_name='Import Context'),
        ),
        migrations.AddField(
            model_name='import',
            name='created_by',
            field=models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(class)ss_created', to=settings.AUTH_USER_MODEL, verbose_name='Created By'),
        ),
        migrations.AddField(
            model_name='import',
            name='last_edited_by',
            field=models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(class)ss_last_edited', to=settings.AUTH_USER_MODEL, verbose_name='Last Edited By'),
        ),
        migrations.CreateModel(
            name='GameMap',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_on', models.DateTimeField(editable=False, null=True, verbose_name='Time of Creation')),
                ('created_on_tz', timezone_field.fields.TimeZoneField(default='Australia/Hobart', editable=False, verbose_name='Time of Creation, Timezone')),
                ('last_edited_on', models.DateTimeField(editable=False, null=True, verbose_name='Time of Last Edit')),
                ('last_edited_on_tz', timezone_field.fields.TimeZoneField(default='Australia/Hobart', editable=False, verbose_name='Time of Last Edit, Timezone')),
                ('theirs', models.CharField(max_length=256, verbose_name='Their Game')),
                ('created_by', models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(class)ss_created', to=settings.AUTH_USER_MODEL, verbose_name='Created By')),
                ('last_edited_by', models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(class)ss_last_edited', to=settings.AUTH_USER_MODEL, verbose_name='Last Edited By')),
                ('ours', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='import_maps', to='Leaderboards.game', verbose_name='Our Game')),
                ('related_import', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='game_maps', to='Import.import', verbose_name='Import')),
            ],
            options={
                'get_latest_by': 'created_on',
                'abstract': False,
            },
        ),
    ]
