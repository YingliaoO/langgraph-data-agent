"""Offline administrator command; never ship the signing key or token to a web bundle."""

import argparse
from datetime import datetime, timedelta, timezone

from jose import jwt
from multisource_data_agent.config import settings


def main():
    parser = argparse.ArgumentParser(
        description="Issue a short-lived token for an explicitly trusted subject"
    )
    parser.add_argument("--subject", required=True)
    parser.add_argument("--minutes", type=int, default=60)
    args = parser.parse_args()
    if not settings.AUTH_JWT_SECRET or len(settings.AUTH_JWT_SECRET.encode()) < 32:
        parser.error("Set a randomly generated AUTH_JWT_SECRET of at least 32 bytes")
    if (
        not args.subject.strip()
        or len(args.subject) > 255
        or any(ord(c) < 32 for c in args.subject)
    ):
        parser.error("Invalid subject")
    if not 1 <= args.minutes <= 1440:
        parser.error("Token lifetime must be 1..1440 minutes")
    now = datetime.now(timezone.utc)
    print(
        jwt.encode(
            {
                "sub": args.subject,
                "iat": now,
                "exp": now + timedelta(minutes=args.minutes),
                "iss": settings.AUTH_JWT_ISSUER,
                "aud": settings.AUTH_JWT_AUDIENCE,
            },
            settings.AUTH_JWT_SECRET,
            algorithm="HS256",
        )
    )


if __name__ == "__main__":
    main()
