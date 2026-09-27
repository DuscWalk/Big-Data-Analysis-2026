"""Small adversarial data with hand-computable outcomes; not demo placeholders."""
DATA = {
    "users": ("1::F::18::4::00100\n1::F::18::4::00100\n1::X::18::4::00100\n"
              "2::M::25::0::90210\n2::F::25::0::90210\n3::M::35::1::ABCD\n"),
    "movies": ("1:: Toy (1995) ::Comedy|Comedy\n2::Other (1996)::Drama\n"
               "3::A (1990)::Drama\n3::B (1990)::Drama\n"),
    "ratings": ("1::1::5::975628799\n01::1::05::975628799\n1::1::6::975628799\n"
                "1::2::4::978307199\n1::2::4::978307200\n2::1::4::978307200\n"
                "1::3::4::978307200\n99::1::3::978307200\n1::1::4::4102444800\n"
                "broken\n1::2::1::980000000\n1::2::5::980000000\n"),
}
EXPECTED_DISPOSITIONS = {
    "users": {"kept": 2, "deduplicated": 1, "quarantined": 3},
    "movies": {"kept": 1, "repaired": 1, "quarantined": 2},
    "ratings": {"kept": 3, "deduplicated": 1, "quarantined": 8},
}
