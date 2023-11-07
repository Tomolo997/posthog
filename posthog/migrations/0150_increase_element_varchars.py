# Generated by Django 3.0.11 on 2021-04-08 14:16

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("posthog", "0149_fix_lifecycle_dashboard_items"),
    ]

    operations = [
        migrations.AlterField(
            model_name="element",
            name="attr_id",
            field=models.CharField(blank=True, max_length=10000, null=True),
        ),
        migrations.AlterField(
            model_name="element",
            name="href",
            field=models.CharField(blank=True, max_length=10000, null=True),
        ),
        migrations.AlterField(
            model_name="element",
            name="tag_name",
            field=models.CharField(blank=True, max_length=1000, null=True),
        ),
        migrations.AlterField(
            model_name="element",
            name="text",
            field=models.CharField(blank=True, max_length=10000, null=True),
        ),
    ]
