# Generated by Django 4.0.3 on 2023-02-24 11:04

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('Import', '0003_alter_import_filename_alter_importcontext_editors'),
        ('Leaderboards', '0002_alter_session_options_alter_backuprating_created_by_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='session',
            name='source',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='sessions', to='Import.import', verbose_name='Source'),
        ),
    ]
