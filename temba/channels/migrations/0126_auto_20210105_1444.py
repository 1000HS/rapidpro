# Generated by Django 2.2.10 on 2021-01-05 14:44

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("channels", "0125_squashed"),
    ]

    operations = [
        migrations.AlterField(
            model_name="channellog", name="id", field=models.BigAutoField(primary_key=True, serialize=False),
        ),
    ]
