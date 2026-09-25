plugins {
    java
    kotlin("jvm") version "2.0.20"
}

group = "com.example.inventory"
version = "1.4.0"

repositories {
    mavenCentral()
}

dependencies {
    implementation(libs.jackson.databind)
    implementation("com.example.inventory:inventory-model:1.4.0")
}
